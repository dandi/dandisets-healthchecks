from __future__ import annotations
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from os.path import getsize
from pathlib import Path
from random import choice
import re
import textwrap
from typing import Optional
import anyio
from .aioutil import pool_tasks
from .config import WORKERS_PER_DANDISET
from .core import (
    Asset,
    AssetPath,
    AssetTestResult,
    DandisetStatus,
    Outcome,
    TestStatus,
    UntestedAsset,
    log,
)
from .tests import TESTS, Test


@dataclass
class TestCase:
    dandiset_id: str
    asset: Asset
    testfunc: Test

    async def run(self) -> AssetTestResult:
        r = await self.testfunc.run(self.asset.filepath)
        log.info(
            "Dandiset %s, asset %s, test %s: %s",
            self.dandiset_id,
            self.asset.asset_path,
            self.testfunc.NAME,
            r.outcome.name,
        )
        return AssetTestResult(
            testname=self.testfunc.NAME,
            asset_path=self.asset.asset_path,
            result=r,
        )


@dataclass
class Untested:
    dandiset_id: str
    asset: Asset

    async def run(self) -> UntestedAsset:
        size = getsize(self.asset.filepath)
        r = await anyio.run_process(["file", "--brief", "-L", str(self.asset.filepath)])
        file_type = r.stdout.decode("utf-8").strip()
        r = await anyio.run_process(
            ["file", "--brief", "--mime-type", "-L", str(self.asset.filepath)]
        )
        mime_type = r.stdout.decode("utf-8").strip()
        log.info(
            "Dandiset %s, asset %s: untested, %d bytes, %s",
            self.dandiset_id,
            self.asset.asset_path,
            size,
            mime_type,
        )
        return UntestedAsset(
            asset=self.asset.asset_path,
            size=size,
            file_type=file_type,
            mime_type=mime_type,
        )


@dataclass
class HealthStatus:
    backup_root: Path
    reports_root: Path
    dandisets: tuple[str, ...]
    dandiset_jobs: int
    versions: dict[str, str]

    async def run_all(self) -> None:
        async def dowork(dandiset: Dandiset) -> None:
            (await dandiset.test_all_assets()).dump()

        await pool_tasks(dowork, self.aiterdandisets(), self.dandiset_jobs)

    async def run_random_assets(self, mode: str) -> None:
        if mode == "random-asset":
            tester = Dandiset.test_random_asset
        elif mode == "random-outdated-asset-first":
            tester = Dandiset.test_random_outdated_asset_first
        else:
            raise ValueError(f"Invalid random asset mode: {mode!r}")

        async def dowork(dandiset: Dandiset) -> None:
            report = await tester(dandiset)
            if report is not None:
                report.dump(await dandiset.get_asset_paths())

        await pool_tasks(dowork, self.aiterdandisets(), self.dandiset_jobs)

    async def aiterdandisets(self) -> AsyncGenerator[Dandiset, None]:
        if self.dandisets:
            for did in self.dandisets:
                yield await self.get_dandiset(
                    did, self.backup_root / "dandisets" / did / "draft"
                )
        else:
            async for p in anyio.Path(self.backup_root / "dandisets").iterdir():
                if re.fullmatch(r"\d{6,}", p.name) and await p.is_dir():
                    log.info("Found Dandiset %s", p.name)
                    yield await self.get_dandiset(p.name, Path(p, "draft"))

    async def get_dandiset(self, identifier: str, path: Path) -> Dandiset:
        return Dandiset(
            identifier=identifier,
            path=path,
            reports_root=self.reports_root,
            versions=self.versions,
        )


@dataclass
class Dandiset:
    identifier: str
    path: Path
    draft_mtime: datetime = field(init=False)
    reports_root: Path
    versions: dict[str, str]

    def __post_init__(self) -> None:
        mtime = self.path.stat().st_mtime
        self.draft_mtime = datetime.fromtimestamp(mtime, timezone.utc)

    @property
    def reportdir(self) -> Path:
        return self.reports_root / "results" / self.identifier

    @property
    def statusfile(self) -> Path:
        return self.reportdir / "status.yaml"

    def load_status(self) -> DandisetStatus:
        return DandisetStatus.from_file(self.identifier, self.statusfile)

    def dump_status(self, status: DandisetStatus) -> None:
        self.statusfile.parent.mkdir(parents=True, exist_ok=True)
        status.to_file(self.statusfile)

    async def test_all_assets(self) -> DandisetReport:
        log.info("Processing Dandiset %s", self.identifier)
        report = DandisetReport(dandiset=self)

        async def dowork(job: TestCase | Untested) -> None:
            res = await job.run()
            if isinstance(res, AssetTestResult):
                report.register_test_result(res)
            else:
                assert isinstance(res, UntestedAsset)
                report.register_untested(res)

        async def aiterassets() -> AsyncGenerator[TestCase | Untested, None]:
            async for asset in self.aiterassets():
                log.info(
                    "Dandiset %s: found asset %s", self.identifier, asset.asset_path
                )
                report.nassets += 1
                if asset.is_nwb():
                    for t in TESTS:
                        yield TestCase(
                            asset=asset, testfunc=t, dandiset_id=self.identifier
                        )
                else:
                    yield Untested(asset=asset, dandiset_id=self.identifier)

        await pool_tasks(dowork, aiterassets(), WORKERS_PER_DANDISET)
        report.finished()
        return report

    async def test_random_asset(self) -> Optional[AssetReport]:
        log.info("Scanning Dandiset %s", self.identifier)
        all_nwbs = [asset async for asset in self.aiterassets() if asset.is_nwb()]
        if all_nwbs:
            return await self.test_one_asset(choice(all_nwbs))
        else:
            log.info("Dandiset %s: no NWB assets", self.identifier)
            return None

    async def test_random_outdated_asset_first(self) -> Optional[AssetReport]:
        try:
            status = self.load_status()
        except FileNotFoundError:
            asset_paths = set()
        else:
            asset_paths = {
                path for t in status.tests for path in t.outdated_assets(self.versions)
            }
        if asset_paths:
            p = choice(list(asset_paths))
            asset = Asset(filepath=self.path / p, asset_path=p)
            return await self.test_one_asset(asset)
        else:
            log.info(
                "Dandiset %s: no outdated assets in status.yaml; selecting from"
                " all assets on disk",
                self.identifier,
            )
            return await self.test_random_asset()

    async def test_one_asset(self, asset: Asset) -> AssetReport:
        report = AssetReport(dandiset=self)

        async def dowork(job: TestCase) -> None:
            report.register_test_result(await job.run())

        async def aiterjobs() -> AsyncGenerator[TestCase, None]:
            for t in TESTS:
                yield TestCase(asset=asset, testfunc=t, dandiset_id=self.identifier)

        await pool_tasks(dowork, aiterjobs(), WORKERS_PER_DANDISET)
        return report

    async def aiterassets(self) -> AsyncIterator[Asset]:
        def mkasset(filepath: anyio.Path) -> Asset:
            return Asset(
                filepath=Path(filepath),
                asset_path=AssetPath(filepath.relative_to(self.path).as_posix()),
            )

        dirs = deque([anyio.Path(self.path)])
        while dirs:
            async for p in dirs.popleft().iterdir():
                if p.name in (
                    ".dandi",
                    ".datalad",
                    ".git",
                    ".gitattributes",
                    ".gitmodules",
                ):
                    continue
                if await p.is_dir():
                    if p.suffix in (".zarr", ".ngff"):
                        yield mkasset(p)
                    else:
                        dirs.append(p)
                elif p.name != "dandiset.yaml":
                    yield mkasset(p)

    async def get_asset_paths(self) -> set[AssetPath]:
        log.info("Scanning Dandiset %s", self.identifier)
        return {asset.asset_path async for asset in self.aiterassets()}


@dataclass
class DandisetReport:
    dandiset: Dandiset
    nassets: int = 0
    tests: dict[str, TestReport] = field(
        default_factory=lambda: defaultdict(TestReport)
    )
    untested: list[UntestedAsset] = field(default_factory=list)
    started: datetime = field(default_factory=lambda: datetime.now().astimezone())
    ended: Optional[datetime] = None

    def register_test_result(self, r: AssetTestResult) -> None:
        self.tests[r.testname].by_outcome[r.outcome].append(r)

    def register_untested(self, d: UntestedAsset) -> None:
        self.untested.append(d)

    def finished(self) -> None:
        self.ended = datetime.now().astimezone()

    def as_status(self) -> DandisetStatus:
        assert self.ended is not None
        return DandisetStatus(
            dandiset=self.dandiset.identifier,
            draft_modified=self.dandiset.draft_mtime,
            last_run=self.started,
            last_run_ended=self.ended,
            last_run_duration=(self.ended - self.started).total_seconds(),
            nassets=self.nassets,
            versions=self.dandiset.versions,
            tests=[
                TestStatus(
                    name=name,
                    assets_ok=sorted(r.asset_path for r in self.tests[name].passed),
                    assets_nok=sorted(r.asset_path for r in self.tests[name].failed),
                    assets_timeout=sorted(
                        r.asset_path for r in self.tests[name].timedout
                    ),
                )
                for name in TESTS.keys()
                if name in self.tests
            ],
            untested=self.untested,
        )

    def dump(self) -> None:
        self.dandiset.dump_status(self.as_status())
        for testname, report in self.tests.items():
            if report.failed:
                with (
                    self.dandiset.reportdir
                    / f"{self.started:%Y.%m.%d.%H.%M.%S}_{testname}_errors.log"
                ).open("w", encoding="utf-8", errors="surrogateescape") as fp:
                    for r in report.failed:
                        assert r.output is not None
                        print(
                            f"Asset: {r.asset_path}\nOutput:\n"
                            + textwrap.indent(r.output, " " * 4),
                            file=fp,
                        )


@dataclass
class TestReport:
    by_outcome: dict[Outcome, list[AssetTestResult]] = field(
        init=False, default_factory=lambda: defaultdict(list)
    )

    @property
    def passed(self) -> list[AssetTestResult]:
        return self.by_outcome[Outcome.PASS]

    @property
    def failed(self) -> list[AssetTestResult]:
        return self.by_outcome[Outcome.FAIL]

    @property
    def timedout(self) -> list[AssetTestResult]:
        return self.by_outcome[Outcome.TIMEOUT]


@dataclass
class AssetReport:
    dandiset: Dandiset
    results: list[AssetTestResult] = field(default_factory=list)
    started: datetime = field(default_factory=lambda: datetime.now().astimezone())

    def register_test_result(self, r: AssetTestResult) -> None:
        self.results.append(r)

    def dump(self, asset_paths: set[AssetPath]) -> None:
        try:
            status = self.dandiset.load_status()
        except FileNotFoundError:
            status = DandisetStatus(
                dandiset=self.dandiset.identifier,
                draft_modified=self.dandiset.draft_mtime,
                tests=[TestStatus(name=testname) for testname in TESTS.keys()],
                versions=self.dandiset.versions,
            )
        for r in self.results:
            status.update_asset(r, self.dandiset.versions)
        status.retain(asset_paths, self.dandiset.versions)
        self.dandiset.dump_status(status)
        for r in self.results:
            if r.outcome is Outcome.FAIL:
                with (
                    self.dandiset.reportdir
                    / f"{self.started:%Y.%m.%d.%H.%M.%S}_{r.testname}_errors.log"
                ).open("a", encoding="utf-8", errors="surrogateescape") as fp:
                    assert r.output is not None
                    print(
                        f"Asset: {r.asset_path}\nOutput:\n"
                        + textwrap.indent(r.output, " " * 4),
                        file=fp,
                    )
