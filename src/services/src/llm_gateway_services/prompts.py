from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from jinja2 import StrictUndefined, TemplateError, nodes
from jinja2.sandbox import SandboxedEnvironment
from llm_gateway_core.contracts import GatewayError, Message, Text, Tool
from llm_gateway_core.structured import check_schema, validate

ALLOWED_NODES = (
    nodes.Template,
    nodes.Output,
    nodes.TemplateData,
    nodes.Name,
    nodes.If,
    nodes.Const,
    nodes.Compare,
    nodes.Operand,
    nodes.And,
    nodes.Or,
    nodes.Not,
)


class PromptStore:
    def __init__(self, root, index):
        self.root, self.index = Path(root).resolve(), Path(index)
        self.bundles = {}
        self.env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

    def template(self, source):
        tree = self.env.parse(source)
        if any(not isinstance(n, ALLOWED_NODES) for n in (tree, *tree.find_all(nodes.Node))):
            raise GatewayError("PROMPT_VARIABLE_INVALID", "Template contains forbidden syntax")
        return self.env.from_string(source)

    def bundle_bytes(self, directory):
        base = (self.root / directory).resolve()
        if not base.is_relative_to(self.root) or not base.is_dir():
            raise GatewayError("REQUEST_INVALID", "Bundle path outside allowed artifact root")
        files, size = {}, 0
        for path in sorted(base.rglob("*")):
            if path.is_symlink():
                raise GatewayError("REQUEST_INVALID", "Bundle symlinks forbidden")
            if path.is_file():
                size += path.stat().st_size
                if size > 1048576 or len(files) >= 64:
                    raise GatewayError("REQUEST_INVALID", "Bundle exceeds artifact size limit")
                files[path.relative_to(base).as_posix()] = path.read_bytes()
        h = hashlib.sha256()
        for name, data in files.items():
            h.update(name.encode() + b"\0" + data + b"\0")
        return files, h.hexdigest()

    def refresh(self, teams=None):
        if self.index.stat().st_size > 1048576:
            raise GatewayError("REQUEST_INVALID", "Prompt index too large")
        entries = json.loads(self.index.read_text())
        pending = dict(self.bundles) if teams is not None else {}
        for entry in entries:
            if teams is not None and entry["owner_team_id"] not in teams:
                continue
            files, digest = self.bundle_bytes(entry["artifact"])
            if digest != entry["hash"]:
                raise GatewayError("REQUEST_INVALID", "Bundle hash mismatch")
            manifest = yaml.safe_load(files["manifest.yaml"])
            key = (manifest["name"], manifest["version"])
            # Version and artifact names are never accepted as filesystem paths from callers.
            from llm_gateway_core.contracts import PromptRef

            PromptRef(name=key[0], version=key[1])
            if key in self.bundles and self.bundles[key]["hash"] != digest:
                raise GatewayError("REQUEST_INVALID", "Published prompt version is immutable")
            bundle = {"manifest": manifest, "hash": digest, "grant": entry}
            for name in ("input.schema.json", "output.schema.json"):
                schema = json.loads(files[name]) if name in files else None
                if schema:
                    check_schema(schema)
                bundle[name] = schema
            bundle["system"] = self.template(files["system.jinja2"].decode())
            bundle["user"] = self.template(files["user.jinja2"].decode())
            bundle["tools"] = [
                Tool.model_validate(t) for t in json.loads(files.get("tools.lock.json", b"[]"))
            ]
            for tool in bundle["tools"]:
                check_schema(tool.parameters)
            bundle["few_shots"] = [
                Message.model_validate(m) for m in json.loads(files.get("few_shots.json", b"[]"))
            ]
            pending[key] = bundle
        self.bundles = pending  # all-or-nothing installation
        return [{"name": n, "version": v, "hash": b["hash"]} for (n, v), b in pending.items()]

    def render(self, request, identity):
        ref = request.prompt
        bundle = self.bundles.get((ref.name, ref.version))
        if not bundle:
            raise GatewayError("PROMPT_NOT_FOUND")
        grant = bundle["grant"]
        if (
            grant["owner_team_id"] != identity.team_id
            or identity.application_id not in grant["applications"]
            or request.logical_model not in grant["use_cases"]
        ):
            raise GatewayError("ACCESS_DENIED", "Prompt bundle is not authorized")
        if bundle["input.schema.json"]:
            validate(ref.variables, bundle["input.schema.json"], "PROMPT_VARIABLE_INVALID")
        if len(json.dumps(ref.variables)) > 65536:
            raise GatewayError("PROMPT_VARIABLE_INVALID", "Variables exceed size limit")
        try:
            system = bundle["system"].render(**ref.variables)
            user = bundle["user"].render(**ref.variables)
        except TemplateError as exc:
            raise GatewayError("PROMPT_VARIABLE_INVALID", "Template rendering failed") from exc
        if len((system + user).encode()) > 262144:
            raise GatewayError("PROMPT_VARIABLE_INVALID", "Rendered prompt exceeds size limit")
        messages = [
            Message(role="system", content=[Text(text=system)]),
            *bundle["few_shots"],
            Message(role="user", content=[Text(text=user)]),
        ]
        return messages, bundle
