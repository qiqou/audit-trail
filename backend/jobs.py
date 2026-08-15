"""本地持久任务执行器。

任务元数据存放在项目 SQLite，而执行线程只负责运行。这样进程退出后，任务记录、
进度和错误仍可被审计人员查看；后续可在同一接口下替换为单工作线程队列。
"""

from __future__ import annotations

import threading
import time
from collections import deque
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
    """每个项目一个 FIFO 工作线程的本地任务执行器。

    备份、全量扫描等任务会同时读写项目数据库和附件库；为同一项目逐个执行，
    比依赖 SQLite 的写锁更可控，也能让 UI 明确显示「排队中」。不同项目仍可
    并行，不会互相阻塞。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cancellations: dict[tuple[str, str], threading.Event] = {}
        self._queues: dict[str, deque[tuple[AuditProject, str, JobHandler]]] = {}
        self._workers: set[str] = set()

    def submit(self, project: AuditProject, job_id: str, handler: JobHandler) -> dict:
        """加入项目队列并立即返回持久化任务记录。

        不在这里直接领取任务：若前一任务仍在运行，后续任务必须保持 queued，
        直到工作线程按 FIFO 顺序领取。这样取消排队任务也不会启动其处理器。
        """
        job = project.get_job(job_id)
        if job is None:
            raise KeyError("任务不存在")
        if job["status"] != AuditProject.JOB_QUEUED:
            return job

        project_key = str(project.root.resolve())
        with self._lock:
            queue = self._queues.setdefault(project_key, deque())
            queue.append((project, job_id, handler))
            if project_key in self._workers:
                return job
            self._workers.add(project_key)

        threading.Thread(
            target=self._run_project_queue,
            args=(project_key,),
            name=f"audit-project-queue-{project.root.name[:24]}",
            daemon=True,
        ).start()
        return job

    def _run_project_queue(self, project_key: str) -> None:
        """一个项目一个工作线程；任务结束后才领取下一项。"""
        while True:
            with self._lock:
                queue = self._queues.get(project_key)
                if not queue:
                    self._queues.pop(project_key, None)
                    self._workers.discard(project_key)
                    return
                project, job_id, handler = queue.popleft()

            # 取消的排队任务已在数据库内标记 cancelled，不能再执行处理器。
            job = project.start_job(job_id)
            if job is None or job["status"] != AuditProject.JOB_RUNNING:
                continue

            key = (project_key, job_id)
            cancel_event = threading.Event()
            with self._lock:
                self._cancellations[key] = cancel_event
            ctx = JobContext(project, job_id, cancel_event)
            try:
                result = handler(ctx)
                project.finish_job(job_id, result=result)
            except Exception as exc:  # 任务错误必须落项目库，不能只出现在后台线程 stderr
                project.finish_job(job_id, error=str(exc))
            finally:
                with self._lock:
                    self._cancellations.pop(key, None)

    def run_and_wait(
        self, project: AuditProject, job_type: str, payload: dict | None, handler: JobHandler,
        *, max_wait_seconds: int = 3600,
    ) -> dict:
        """将一个需即时返回的旧接口也纳入项目串行队列。

        打包、手工备份和合并当前仍需在同一个 HTTP 请求中返回下载地址或合并
        报告，不能直接改成异步接口。该适配层保留现有前端契约，但避免它们绕过
        自动备份/扫描的项目队列；任务记录和最终错误仍会持久化到 SQLite。
        I5：等待有界（默认 1 小时），超时与取消均给出明确错误而非无限挂起。
        """
        job = project.create_job(job_type, payload)
        completed = threading.Event()
        outcome: dict[str, object] = {}

        def wrapped(ctx: JobContext) -> dict:
            try:
                result = handler(ctx)
                outcome["result"] = result
                return result
            except Exception as exc:
                outcome["error"] = exc
                raise
            finally:
                completed.set()

        self.submit(project, job["id"], wrapped)
        # 排队时若被别的入口取消，wrapped 不会被调用；轮询持久化状态以免
        # 同步接口永远等待一个已取消任务。同步接口不得无限挂起队列。
        deadline = time.monotonic() + max_wait_seconds
        while not completed.wait(timeout=0.1):
            if time.monotonic() > deadline:
                raise InterruptedError(
                    "任务执行时间过长，已停止同步等待；请稍后在任务列表中查看结果"
                )
            current = project.get_job(job["id"])
            if current and current["status"] == AuditProject.JOB_CANCELLED:
                raise InterruptedError("任务已取消")
        error = outcome.get("error")
        if isinstance(error, Exception):
            raise error
        result = outcome.get("result")
        if not isinstance(result, dict):
            raise TypeError("任务未返回有效结果")
        return result

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

    def has_active(self, project: AuditProject) -> bool:
        """供回收站等破坏性入口拒绝与备份/扫描/合并并发执行。"""
        return any(
            job["status"] in {AuditProject.JOB_QUEUED, AuditProject.JOB_RUNNING}
            for job in project.list_jobs(limit=500)
        )

    def cancel_all(self, project: AuditProject) -> int:
        """请求取消一个项目全部活动任务，供退出/受控关闭使用。"""
        cancelled = 0
        for job in project.list_jobs(limit=500):
            if job["status"] in {AuditProject.JOB_QUEUED, AuditProject.JOB_RUNNING} and self.cancel(project, job["id"]) is not None:
                cancelled += 1
        return cancelled

    def wait_until_idle(self, project: AuditProject, timeout: float = 10.0) -> bool:
        """有界等待取消检查点；超时由调用方保留连接，不能强关仍在工作的项目。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while self.has_active(project):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True


job_runner = ProjectJobRunner()
