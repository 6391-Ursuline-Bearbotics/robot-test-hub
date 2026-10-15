"""Independent close-error classification after capture child reaping."""
from types import SimpleNamespace
import unittest

from robot_test_hub.recorder import FFmpegAdapter


class RecorderCloseIndependentReviewTest(unittest.TestCase):
    class Stream:
        def __init__(self,error=None,release=True):
            self.error,self.release=error,release
            self.closed=False
            self.calls=0
        def close(self):
            self.calls+=1
            if self.release:
                self.closed=True
            if self.error:
                raise self.error('synthetic close failure')

    def adapter(self,stdin,stdout=None):
        class Reaped:
            def poll(inner):return 0
            def wait(inner,timeout):return 0
        adapter=FFmpegAdapter(SimpleNamespace(operation_timeout=.1))
        adapter.process=Reaped()
        adapter.process.stdin=stdin
        adapter.process.stdout=stdout or self.Stream()
        adapter.process.stderr=self.Stream()
        return adapter

    def test_released_stdin_broken_pipe_still_closes_both_read_pipes(self):
        stdin=self.Stream(BrokenPipeError)
        adapter=self.adapter(stdin)
        adapter.stop()
        self.assertTrue(stdin.closed)
        self.assertTrue(adapter.process.stdout.closed)
        self.assertTrue(adapter.process.stderr.closed)
        self.assertEqual(stdin.calls,1)

    def test_broken_pipe_from_even_closed_read_pipe_is_not_suppressed(self):
        stdout=self.Stream(BrokenPipeError)
        adapter=self.adapter(self.Stream(),stdout)
        with self.assertRaises(BrokenPipeError):
            adapter.stop()
        self.assertTrue(stdout.closed)
        self.assertFalse(adapter.process.stderr.closed)

    def test_open_stdin_and_other_os_errors_remain_cleanup_failures(self):
        for error,release in ((BrokenPipeError,False),(OSError,True),(PermissionError,True)):
            with self.subTest(error=error,release=release):
                stdin=self.Stream(error,release)
                adapter=self.adapter(stdin)
                with self.assertRaises(error):
                    adapter.stop()
                self.assertFalse(adapter.process.stdout.closed)
                self.assertFalse(adapter.process.stderr.closed)
    def test_windows_einval_from_released_stdin_completes_cleanup(self):
        import errno
        from unittest.mock import patch
        from robot_test_hub import recorder as recorder_module
        stdin=self.Stream(lambda message:OSError(errno.EINVAL,message))
        adapter=self.adapter(stdin)
        with patch.object(recorder_module,'os',SimpleNamespace(name='nt')):
            adapter.stop()
        self.assertTrue(stdin.closed)
        self.assertTrue(adapter.process.stdout.closed)
        self.assertTrue(adapter.process.stderr.closed)

    def test_posix_einval_is_not_suppressed_even_for_closed_stdin(self):
        import errno
        from unittest.mock import patch
        from robot_test_hub import recorder as recorder_module
        adapter=self.adapter(self.Stream(lambda message:OSError(errno.EINVAL,message)))
        with patch.object(recorder_module,'os',SimpleNamespace(name='posix')):
            with self.assertRaises(OSError) as raised:
                adapter.stop()
        self.assertEqual(raised.exception.errno,errno.EINVAL)
        self.assertFalse(adapter.process.stdout.closed)

    def test_windows_einval_open_stdin_read_pipe_and_other_errors_still_fail(self):
        import errno
        from unittest.mock import patch
        from robot_test_hub import recorder as recorder_module
        for target,release,code in (('stdin',False,errno.EINVAL),('stdout',True,errno.EINVAL),
                                    ('stdin',True,errno.EIO),('stdin',True,errno.EACCES)):
            with self.subTest(target=target,release=release,code=code):
                stream=self.Stream(lambda message:OSError(code,message),release)
                adapter=self.adapter(stream if target=='stdin' else self.Stream(),stream if target=='stdout' else None)
                with patch.object(recorder_module,'os',SimpleNamespace(name='nt')):
                    with self.assertRaises(OSError) as raised:
                        adapter.stop()
                self.assertEqual(raised.exception.errno,code)
                self.assertFalse(adapter.process.stderr.closed)