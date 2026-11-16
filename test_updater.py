import contextlib
import io
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from types import SimpleNamespace
import tempfile
import json

class ProducerTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).with_name('crystal_ball_updater.py')
        self.assertTrue(path.exists(), 'standalone updater missing')
        import crystal_ball_updater
        self.m=crystal_ball_updater

    def test_producer_substitution_does_not_invoke_shell(self):
        start=datetime(2026,11,16,9,30,tzinfo=timezone.utc)
        result=self.m.producer_command(['python','generator.py','--start','{start}',
            '--duration','{duration}','--output','{output}','$(literal)'], start,20,Path('/tmp/a b.mp4'))
        self.assertEqual(result[-1],'$(literal)')
        self.assertEqual(result[-2],'/tmp/a b.mp4')
        self.assertEqual(result[3],start.isoformat())

    def test_default_clock_and_thirty_minute_interval(self):
        a=self.m.parse_args(['--uuid','EXAMPLE','--hub','1','--port','2'])
        self.assertEqual(a.interval_minutes,30)
        self.assertEqual(a.duration_minutes,35)
        self.assertIn('generate_clock_video.py',a.producer_command[1])
        self.assertIn('{start}',a.producer_command)
        self.assertIn('{output}',a.producer_command)

    def test_missing_output_placeholder_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            self.m.parse_args(['--uuid','EXAMPLE','--hub','1','--port','2',
              '--producer-command-json','["python", "demo.py"]'])

    def test_config_file_loads_producer_and_interval(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config.json'
            path.write_text(json.dumps({'uuid':'EXAMPLE','hub':'1','port':'2',
                'producer_command':['demo','{output}'],'interval_minutes':15}))
            a=self.m.parse_args(['--config',str(path)])
            self.assertEqual(a.producer_command,['demo','{output}'])
            self.assertEqual(a.interval_minutes,15)

    def test_second_instance_exits_without_touching_state(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            args=SimpleNamespace(state_dir=Path(d),cycles=1,interval_minutes=15,lead_minutes=3)
            with open(Path(d)/'refresh.lock','a') as held, \
                 patch.object(self.m,'parse_args',return_value=args), \
                 patch.object(self.m,'cycle') as cycles, self.assertLogs('crystal-updater','ERROR'):
                fcntl.flock(held,fcntl.LOCK_EX)
                self.assertEqual(self.m.main(),1)
            cycles.assert_not_called()
            self.assertFalse((Path(d)/'last_failure.json').exists())

    def test_config_value_cannot_inject_an_option(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config.json'
            path.write_text(json.dumps({'uuid':'EXAMPLE','hub':'1','port':'2',
                'filename':'--pi5-shared-power'}))
            with contextlib.redirect_stderr(io.StringIO()) as err,self.assertRaises(SystemExit):
                self.m.parse_args(['--config',str(path)])
            self.assertIn('filename must be',err.getvalue())

    def test_video_too_short_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            movie=Path(d)/'movie.mp4';movie.write_bytes(b'x')
            meta=json.dumps({'format':{'duration':'60'},'streams':[{'codec_type':'video'}]})
            with patch.object(self.m,'command',return_value=meta):
                with self.assertRaisesRegex(RuntimeError,'too short'):
                    self.m.validate_video(movie,20)

    def test_two_cycles_target_exact_fifteen_minute_gap(self):
        with tempfile.TemporaryDirectory() as d:
            first=datetime.now().astimezone()-timedelta(minutes=15)
            args=SimpleNamespace(state_dir=Path(d),cycles=2,interval_minutes=15,lead_minutes=3)
            with patch.object(self.m,'parse_args',return_value=args), \
                 patch.object(self.m,'cycle',side_effect=[first,first+timedelta(minutes=15)]) as cycles:
                self.assertEqual(self.m.main(),0)
            self.assertEqual(cycles.call_args_list[1].args[1],first+timedelta(minutes=15))

if __name__=='__main__':unittest.main()
