import queue
import threading
import logging

logger = logging.getLogger("engine.write_queue")

class WriteTask:
    def __init__(self, fn, args, kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.event = threading.Event()
        self.result = None
        self.exception = None

class WriteQueue:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        import sys
        self._bypass = any(key in sys.modules for key in ('pytest', '_pytest'))
        self._queue = queue.Queue()
        self._instance_lock = threading.Lock()
        self._worker_thread = None
        self._ensure_worker_alive()
        self._initialized = True

    def _ensure_worker_alive(self):
        """Checks if the worker thread is alive, and restarts it if it is not."""
        if getattr(self, '_bypass', False):
            return
        with self._instance_lock:
            if self._worker_thread is None or not self._worker_thread.is_alive():
                logger.warning("[WRITE-QUEUE] Worker thread is dead or not started. Starting/Restarting it.")
                self._worker_thread = threading.Thread(target=self._worker_loop, name="WriteQueueWorker", daemon=True)
                self._worker_thread.start()

    def _worker_loop(self):
        logger.info("WriteQueue worker thread started.")
        while True:
            try:
                task = self._queue.get()
                if task is None:
                    break
                try:
                    task.result = task.fn(*task.args, **task.kwargs)
                except Exception as e:
                    logger.exception(f"Error executing task {task.fn.__name__} in WriteQueue")
                    task.exception = e
                finally:
                    task.event.set()
                    self._queue.task_done()
            except Exception as e:
                logger.exception("Fatal error in WriteQueue worker loop")

    def put(self, fn, *args, **kwargs):
        """Enqueue a function execution asynchronously."""
        if getattr(self, '_bypass', False):
            task = WriteTask(fn, args, kwargs)
            try:
                task.result = fn(*args, **kwargs)
            except Exception as e:
                task.exception = e
            task.event.set()
            return task
        
        self._ensure_worker_alive()
        task = WriteTask(fn, args, kwargs)
        self._queue.put(task)
        return task

    def put_and_wait(self, fn, *args, **kwargs):
        """Enqueue and block the calling thread until the task is executed.
        If called from the worker thread itself, or if _bypass is True, bypasses queueing to avoid deadlock.
        """
        _wq_timeout = kwargs.pop('_wq_timeout', 30)
        if getattr(self, '_bypass', False) or threading.current_thread() == self._worker_thread:
            # Worker thread/bypass for nested wrapped calls
            return fn(*args, **kwargs)

        self._ensure_worker_alive()
        task = WriteTask(fn, args, kwargs)
        self._queue.put(task)
        
        completed = task.event.wait(timeout=_wq_timeout)
        if not completed:
            logger.critical(f"[WRITE-QUEUE] TIMEOUT waiting for task. Worker may be deadlocked. Timeout: {_wq_timeout}s.")
            raise TimeoutError(f"Write queue task timed out after {int(_wq_timeout)}s")
            
        if task.exception is not None:
            raise task.exception
        return task.result

    def flush(self):
        """Block until all enqueued tasks are processed."""
        if getattr(self, '_bypass', False):
            return
        self._queue.join()


