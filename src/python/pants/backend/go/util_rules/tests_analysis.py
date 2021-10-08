# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import json
import pkgutil
from dataclasses import dataclass
from typing import ClassVar

from pants.backend.go.util_rules.build_pkg import BuildGoPackageRequest, BuiltGoPackage
from pants.backend.go.util_rules.import_analysis import ImportConfig, ImportConfigRequest
from pants.backend.go.util_rules.link import LinkedGoBinary, LinkGoBinaryRequest
from pants.engine.fs import CreateDigest, Digest, FileContent, MergeDigests
from pants.engine.internals.selectors import Get, MultiGet
from pants.engine.process import Process, ProcessResult
from pants.engine.rules import collect_rules, rule
from pants.util.ordered_set import FrozenOrderedSet


@dataclass(frozen=True)
class AnalyzeTestSourcesRequest:
    digest: Digest
    paths: FrozenOrderedSet[str]


@dataclass(frozen=True)
class GoTestCase:
    name: str
    package: str

    @classmethod
    def from_json_dict(cls, data: dict) -> GoTestCase:
        return cls(name=data["name"], package=data["package"])


@dataclass(frozen=True)
class Example:
    name: str
    package: str
    output: str
    unordered: bool

    @classmethod
    def from_json_dict(cls, data: dict) -> Example:
        return cls(
            name=data["name"],
            package=data["package"],
            output=data["output"],
            unordered=data["unordered"],
        )


@dataclass(frozen=True)
class AnalyzedTestSources:
    tests: FrozenOrderedSet[GoTestCase]
    benchmarks: FrozenOrderedSet[GoTestCase]
    examples: FrozenOrderedSet[Example]
    test_main: GoTestCase | None

    @classmethod
    def from_json_dict(cls, data: dict) -> AnalyzedTestSources:
        # Note: The Go `json` package is producing `null` values so the keys may be present but set to `null` so
        # this code uses `data.get("foo") or []` instead of `data.get("foo", []) to handle that case.

        test_main = None
        if "test_main" in data:
            test_main = GoTestCase.from_json_dict(data["test_main"])

        return cls(
            tests=FrozenOrderedSet([GoTestCase.from_json_dict(d) for d in data.get("tests") or []]),
            benchmarks=FrozenOrderedSet(
                [GoTestCase.from_json_dict(d) for d in data.get("benchmarks") or []]
            ),
            examples=FrozenOrderedSet(
                [Example.from_json_dict(d) for d in data.get("examples") or []]
            ),
            test_main=test_main,
        )


@dataclass(frozen=True)
class AnalyzerSetup:
    digest: Digest
    PATH: ClassVar[str] = "./analyzer"


@rule
async def setup_analyzer() -> AnalyzerSetup:
    source_entry_content = pkgutil.get_data(
        "pants.backend.go.util_rules", "analyze_test_sources.go"
    )
    if not source_entry_content:
        raise AssertionError("Unable to find resource for `analyze_test_sources.go`.")

    source_entry = FileContent("analyze_test_sources.go", source_entry_content)

    source_digest, import_config = await MultiGet(
        Get(Digest, CreateDigest([source_entry])),
        Get(ImportConfig, ImportConfigRequest, ImportConfigRequest.stdlib_only()),
    )

    built_analyzer_pkg = await Get(
        BuiltGoPackage,
        BuildGoPackageRequest(
            import_path="main",
            subpath="",
            digest=source_digest,
            go_file_names=(source_entry.path,),
            s_file_names=(),
            direct_dependencies=(),
        ),
    )
    main_pkg_a_file_path = built_analyzer_pkg.import_paths_to_pkg_a_files["main"]
    input_digest = await Get(
        Digest, MergeDigests([built_analyzer_pkg.digest, import_config.digest])
    )

    analyzer = await Get(
        LinkedGoBinary,
        LinkGoBinaryRequest(
            input_digest=input_digest,
            archives=(main_pkg_a_file_path,),
            import_config_path=import_config.CONFIG_PATH,
            output_filename=AnalyzerSetup.PATH,
            description="Link Go test sources analyzer",
        ),
    )

    return AnalyzerSetup(analyzer.digest)


@rule
async def analyze_test_sources(
    request: AnalyzeTestSourcesRequest, analyzer: AnalyzerSetup
) -> AnalyzedTestSources:
    input_digest = await Get(Digest, MergeDigests([request.digest, analyzer.digest]))

    result = await Get(
        ProcessResult,
        Process(
            argv=(analyzer.PATH, *request.paths),
            input_digest=input_digest,
            description="Analyze Go test sources.",
        ),
    )

    metadata = json.loads(result.stdout.decode("utf-8"))
    return AnalyzedTestSources.from_json_dict(metadata)


def rules():
    return collect_rules()
