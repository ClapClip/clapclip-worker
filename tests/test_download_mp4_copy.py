import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mediaworker.downloader.mp4_copy import compatible, number, probe_shape
from mediaworker.downloader.safety import ServiceError
from mediaworker.downloader.worker import Config, ProcessRunner, format_options


def details(*, audio=True):
    data = {"format": {"duration": "2", "start_time": "0"}, "streams": [
        {"codec_type": "video", "codec_name": "h264", "profile": "High", "level": 31, "pix_fmt": "yuv420p", "width": 640,
         "height": 360, "sample_aspect_ratio": "1:1", "start_time": "0", "start_pts": 0,
         "duration": "2", "duration_ts": 25600, "time_base": "1/12800"}]}
    if audio:
        data["streams"].append({"codec_type": "audio", "codec_name": "aac", "profile": "LC", "start_time": "0",
                                "duration": "2", "duration_ts": 96000, "start_pts": 0, "time_base": "1/48000"})
    return data


OPTION = {"audio": False, "public": {"ext": "mp4", "width": 640, "height": 360}}
INPUT = ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm,mpegts,mp3,ogg,aac"]


class Mp4CopyTests(unittest.TestCase):
    def test_unknown_probe_json_shapes_and_numeric_overflow_are_not_copy_candidates(self):
        malformed = [None, [], True, 1, "unknown", {}, {"format":None,"streams":[]},
                     {"format":[],"streams":[]}, {"format":0,"streams":[]},
                     {"format":{},"streams":None}, {"format":{},"streams":"bad"},
                     {"format":{},"streams":[None]}, {"format":{},"streams":[[]]},
                     {"format":{},"streams":[7]}]
        for value in malformed:
            with self.subTest(value=value):
                self.assertFalse(probe_shape(value))
                self.assertFalse(compatible(value,640,360,video=True,audio=False))
        self.assertIsNone(number(10**1000))
        bad = details(); bad['format']['duration'] = 10**1000
        self.assertFalse(compatible(bad,640,360,video=True,audio=True))

    def test_parent_rejects_unknown_first_or_second_source_probe_before_audio_mapping(self):
        malformed = [None, [], {}, {'format':None,'streams':[]}, {'format':[],'streams':[]},
                     {'format':{},'streams':None}, {'format':{},'streams':[None]},
                     {'format':{},'streams':[[]]}, {'format':{},'streams':[7]}]
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            for bad in malformed:
                for bad_index in (0,1):
                    with self.subTest(bad=bad,index=bad_index):
                        calls=[]
                        runner=ProcessRunner(Config(),object())
                        def run(args,*a,**kw):
                            self.assertIn('-show_entries',args)
                            calls.append(args)
                            return json.dumps(bad if len(calls)-1==bad_index else details())
                        runner.run=run
                        result=runner.copy_mp4([folder/'one',folder/'two'],OPTION,folder/'result.mp4',
                            folder,threading.Event(),time.monotonic()+30,INPUT)
                        self.assertFalse(result)
                        self.assertEqual(len(calls),bad_index+1)

    def test_gate_rejects_every_required_conversion_and_unknown_timestamp(self):
        valid = details()
        self.assertTrue(compatible(valid, 640, 360, video=True, audio=True, max_duration=300))
        alterations = [("codec_name", "hevc"), ("pix_fmt", "yuv420p10le"), ("sample_aspect_ratio", "2:1"),
                       ("width", 641), ("width", 1280), ("height", 720), ("start_time", "0.1"),
                       ("duration", "nan"), ("duration", "301"), ("time_base", "0/1"),
                       ("start_pts", 1), ("duration_ts", 3840001),
                       ("profile", "High 10"), ("level", 62), ("level", None),
                       ("tags", {"rotate": "90"}), ("side_data_list", [{"rotation": -90}])]
        for key, value in alterations:
            with self.subTest(key=key, value=value):
                changed = copy.deepcopy(valid); changed["streams"][0][key] = value
                self.assertFalse(compatible(changed, 640, 360, video=True, audio=True, max_duration=300))
        for change in (None, "N/A", "nan", "-1", "301"):
            bad = copy.deepcopy(valid); bad["streams"][1]["duration"] = change
            self.assertFalse(compatible(bad, 640, 360, video=True, audio=True, max_duration=300))
        for codec in ("opus", "mp3", None):
            bad = copy.deepcopy(valid); bad["streams"][1]["codec_name"] = codec
            self.assertFalse(compatible(bad, 640, 360, video=True, audio=True))
        bad = copy.deepcopy(valid); bad['streams'][1]['profile'] = 'HE-AAC'
        self.assertFalse(compatible(bad, 640, 360, video=True, audio=True))
        bad = copy.deepcopy(valid); bad["streams"].append({"codec_type": "subtitle"})
        self.assertFalse(compatible(bad, 640, 360, video=True, audio=True))
        self.assertTrue(compatible(details(audio=False), 640, 360, video=True, audio=False))

    def exercise(self, *, failure=None, output=None, selected=OPTION, cancel=None, deadline=None):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        folder = Path(temp.name); target = folder/'result.mp4'; source = folder/'source.media'; source.touch()
        runner = ProcessRunner(Config(), object())
        events = []
        def run(args, directory, event, timeout, *rest, **kwargs):
            self.assertGreater(timeout, 0)
            events.append(args)
            if '-show_entries' in args:
                return json.dumps(output if Path(args[-1]) == target and output is not None else details())
            if '-c' in args:
                target.write_bytes(b'fixture')
            if failure and (('-f' in args and 'null' in args) or failure.code in {'TIMEOUT', 'CANCELLED'}):
                raise failure
            return ''
        runner.run = run
        result = runner.copy_mp4([source], selected, target, folder, cancel or threading.Event(),
                                 deadline if deadline is not None else time.monotonic()+30, INPUT)
        return result, events, target

    def test_copy_requires_full_decode_and_never_network_input(self):
        result, events, _ = self.exercise()
        self.assertTrue(result)
        self.assertTrue(any('-f' in e and 'null' in e and '-xerror' in e for e in events))
        for args in events:
            if '-i' in args:
                self.assertEqual(args[args.index('-protocol_whitelist')+1], 'file,pipe')
                self.assertFalse(any(str(x).startswith(('http:', 'https:')) for x in args))

    def test_invalid_copy_output_or_decode_error_falls_back_and_removes_output(self):
        bad = details(); bad['streams'][0]['duration'] = '300.001'
        for failure, output in ((None, bad), (ServiceError('MEDIA_UNAVAILABLE', 'fixed', 422), None)):
            result, _, target = self.exercise(failure=failure, output=output)
            self.assertFalse(result); self.assertFalse(target.exists())

    def test_cancel_timeout_and_expired_budget_never_become_encoder_retry(self):
        for code in ('TIMEOUT', 'CANCELLED'):
            with self.subTest(code=code), self.assertRaises(ServiceError) as caught:
                self.exercise(failure=ServiceError(code, 'fixed', 422))
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ServiceError) as caught:
            self.exercise(deadline=time.monotonic()-1)
        self.assertEqual(caught.exception.code, 'TIMEOUT')
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(ServiceError) as caught:
            self.exercise(cancel=cancel)
        self.assertEqual(caught.exception.code, 'CANCELLED')

    def test_selected_shape_and_non_mp4_never_get_copy_path(self):
        small = copy.deepcopy(OPTION); small['public']['width'] = 320
        result, events, _ = self.exercise(selected=small)
        self.assertFalse(result); self.assertFalse(any('-c' in e for e in events))
        for ext, audio in (('webm', False), ('mp3', True)):
            option = {'audio': audio, 'public': {'ext': ext}}
            result, events, _ = self.exercise(selected=option)
            self.assertFalse(result); self.assertEqual(events, [])

    def test_complete_download_does_not_encode_after_cancel_expiry_or_expired_fallback(self):
        for action in ('cancel_after_decode', 'expire_after_decode', 'expire_rejected_copy'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temporary:
                clock, probes, encoded = [0.0], [0], []
                event = threading.Event()
                info = {'duration':2, 'formats':[{'format_id':'v','url':'https://example.invalid/local',
                    'protocol':'https','ext':'mp4','width':640,'height':360,'vcodec':'avc1','acodec':'mp4a'}]}
                class Proxy:
                    port, url = 65534, 'http://unused:unused@127.0.0.1:65534'
                    def allow_media(self): pass
                class Runner(ProcessRunner):
                    def inspect(self,*_): return info
                    def run(self,args,directory,cancel,timeout,*a,**kw):
                        if '--output' in args:
                            Path(args[args.index('--output')+1]).write_bytes(b'source'); return ''
                        if '-show_entries' in args:
                            probes[0] += 1
                            data = details()
                            if action=='expire_rejected_copy' and probes[0]==2:
                                data['streams'][0]['pix_fmt']='yuv420p10le'; clock[0]=300
                            return json.dumps(data)
                        if '-c:v' in args:
                            encoded.append(True); raise AssertionError('encoder must not start')
                        if '-c' in args: Path(args[-1]).write_bytes(b'copy')
                        if '-f' in args and 'null' in args:
                            if action=='cancel_after_decode':event.set()
                            if action=='expire_after_decode':clock[0]=300
                        return ''
                runner = Runner(Config(),Proxy())
                option = next(iter(format_options(info).values()))
                with patch('mediaworker.downloader.worker.time.monotonic',side_effect=lambda:clock[0]):
                    with self.assertRaises(ServiceError) as caught:
                        runner.download({'cancel':event},{'url':'https://example.invalid/local'},option,Path(temporary),lambda *_:None)
                self.assertEqual(caught.exception.code,'CANCELLED' if action.startswith('cancel') else 'TIMEOUT')
                self.assertEqual(encoded,[])
                self.assertEqual(list(Path(temporary).glob('.fresh-component-*')),[])


if __name__ == '__main__':
    unittest.main()
