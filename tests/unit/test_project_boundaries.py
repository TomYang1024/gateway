"""The workspace graph is an executable boundary, not just a folder convention."""

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "project,forbidden",
    [
        (
            "core",
            (
                "llm_gateway_services",
                "llm_gateway_api",
                "llm_gateway_infrastructure",
                "fastapi",
                "httpx",
                "redis",
                "sqlalchemy",
            ),
        ),
        (
            "services",
            ("llm_gateway_api", "llm_gateway_infrastructure", "fastapi", "httpx", "redis", "sqlalchemy"),
        ),
        ("infrastructure", ("llm_gateway_api", "llm_gateway_services", "fastapi")),
    ],
)
def test_project_import_direction(project, forbidden):
    for path in (ROOT / "src" / project / "src").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [n.name for n in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            assert not any(m == p or m.startswith(p + ".") for m in modules for p in forbidden), str(path)


@pytest.mark.parametrize("project", ["core", "services", "infrastructure", "api"])
def test_project_has_own_build_metadata(project):
    config = tomllib.loads((ROOT / "src" / project / "pyproject.toml").read_text())
    assert config["project"]["name"] == f"llm-gateway-{project}"
    assert config["build-system"]["build-backend"] == "hatchling.build"
    assert config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [f"src/llm_gateway_{project}"]


def test_sdk_has_no_server_dependencies():
    config = tomllib.loads((ROOT / "sdk/python/pyproject.toml").read_text())
    assert not any(
        "services" in dep or "infrastructure" in dep or "api==" in dep
        for dep in config["project"]["dependencies"]
    )
