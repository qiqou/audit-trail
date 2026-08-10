"""本地持久任务执行器。

任务元数据存放在项目 SQLite，而执行线程只负责运行。这样进程退出后，任务记录、
进度和错误仍可被审计人员查看；后续可在同一接口下替换为单工作线程队列。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from database import AuditProject


class JobContext:
    """提供任务所需的进度、取消检查点，不暴露数据库连接。"""

    def __init__(self, project: AuditProject, job_id: str, cancel_event: threading.Event):
        self.project = project
        self.job_id = job_id
        self.cancel_event = cancel_event

    def cancelled(self) -> bool:
        if self.cancel_event.is_set() or self.project.is_job_cancel_requested(self.job_id):
            self.cancel_event.set()
            return True
        return False

    def progress(self, done: int, total: int, phase: str) -> None:
        self.project.update_job_progress(
            self.job_id,
            {"phase": phase, "done": max(0, int(done)), "total": max(0, int(total))},
        )


JobHandler = Callable[[JobContext], dict]


class ProjectJobRunner:
    """单进程任务执行器；每个项目任务均以 project + job_id 为唯一键。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._cancellations: dict[tuple[str, str], threading.Event] = {}

    def submit(self, project: AuditProject, job_id: str, handler: JobHandler) -> dict:
        job = project.start_job(job_id)
        if job is None:
            raise KeyError("任务不存在")
        if job["status"] != AuditProject.JOB_RUNNING:
            return job

        key = (str(project.root.resolve()), job_id)
        cancel_event = threading.Event()
        with self._lock:
            self._cancellations[key] = cancel_event

        def run() -> None:
            ctx = JobContext(project, job_id, cancel_event)
            try:
                result = handler(ctx)
                project.finish_job(job_id, result=result)
            except Exception as exc:  # 任务错误必须落项目库，不能只出现在后台线程 stderr
                project.finish_job(job_id, error=str(exc))
            finally:
                with self._lock:
                    self._cancellations.pop(key, None)

        threading.Thread(target=run, name=f"audit-job-{job_id[:8]}", daemon=True).start()
        return job

    def cancel(self, project: AuditProject, job_id: str) -> dict | None:
        job = project.request_job_cancel(job_id)
        if job is None:
            return None
        key = (str(project.root.resolve()), job_id)
        with self._lock:
            event = self._cancellations.get(key)
        if event is not None:
            event.set()
        return job


job_runner = ProjectJobRunner()
