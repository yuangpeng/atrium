#!/usr/bin/env python3
"""Probe a models gateway and keep Atrium's extra_models lists live.

Harnesses only offer the models their own CLI knows about. Behind a gateway
(a relay, a company proxy) the useful models are elsewhere, and which of them
answer changes through the day: one goes offline, another loses its routing.
This script probes the models named in its config with the cheapest request
each API allows, and writes the ones that answered into Atrium's settings as
``harnesses.<id>.extra_models``. Atrium re-reads settings.json when it changes,
so the model pickers follow the gateway without a restart.

Run it from a systemd timer (or cron) on the host Atrium runs on:

    [Service]
    Type=oneshot
    ExecStart=/usr/bin/python3 %h/bin/atrium-model-probe.py

    [Timer]
    OnUnitActiveSec=15min

Config, ``~/.atrium/model-probe.json`` (re-read on every run, as is this file):

    {
      "base_url": "https://gateway.example.com",
      "gemini_base_url": "https://gateway.example.com/gemini",
      "api_key_env": "MODELS_GATEWAY_KEY",
      "api_key_file": "~/.config/gateway/config.json",
      "api_key_file_key": "apiKey",
      "models": {
        "claude-code": ["claude-opus-4-5-20251101"],
        "codex": ["gpt-5.1-codex"],
        "antigravity": ["gemini-3-pro", "!gemini-3-flash"]
      },
      "antigravity_labels": {"gemini-3-pro": "Gemini 3 Pro (gw)"}
    }

- the key comes from ``api_key_env`` if that variable is set, else from
  ``api_key_file`` (a JSON file, read at ``api_key_file_key``).
- ``!`` before a model pins it dead without spending a probe on it: for one
  the gateway lists and routes, but whose upstream rejects the real calls.
- a model probes through the harness's own API: ``/v1/messages`` with
  ``max_tokens`` 1 for Claude Code (~10 input + 1 output token), ``/v1/responses``
  with a one-word ``instructions`` for Codex (~13 + 5; without it a gateway may
  prepend the CLI's own multi-thousand-token system prompt), and streaming
  ``generateContent`` for Antigravity (~6 + 1) — the path its sessions take, so
  an upstream that only fails on streaming is caught here.
- Antigravity has no extra_models: its models are the custom models of
  ``~/.gemini/antigravity-cli/settings.json``, which this script rewrites. A dead
  one keeps its entry, labelled ``[offline]``: removing it would leave sessions
  that still name it resolving the bare key as a model id on Google's own API,
  which then times out instead of saying what is wrong.
"""

import concurrent.futures
import json
import os
import sys
import urllib.request

HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".atrium", "settings.json")
CONFIG = os.path.join(HOME, ".atrium", "model-probe.json")
ANTIGRAVITY_SETTINGS = os.path.join(HOME, ".gemini", "antigravity-cli", "settings.json")
TIMEOUT = 45
CODEX_EFFORTS = {"efforts": ["low", "medium", "high", "xhigh"], "default_effort": "high"}


def api_key(cfg):
    env = cfg.get("api_key_env")
    if env and os.environ.get(env):
        return os.environ[env]
    path = cfg.get("api_key_file")
    if path:
        data = json.load(open(os.path.expanduser(path)))
        return data[cfg.get("api_key_file_key", "apiKey")]
    raise SystemExit("model-probe: no api_key_env in the environment and no api_key_file")


def probe(kind, model, key, cfg):
    """True when the model answers a minimal request."""
    if kind == "antigravity":
        base = cfg.get("gemini_base_url") or cfg["base_url"] + "/gemini"
        url = base + "/v1beta/models/" + model + ":streamGenerateContent?alt=sse"
        body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "generationConfig": {"maxOutputTokens": 1}}
        headers = {"x-goog-api-key": key}
    elif kind == "claude-code":
        url = cfg["base_url"] + "/v1/messages"
        body = {"model": model, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
        headers = {"Authorization": "Bearer " + key, "anthropic-version": "2023-06-01"}
    else:
        url = cfg["base_url"] + "/v1/responses"
        body = {"model": model, "input": "hi", "instructions": "reply ok",
                "max_output_tokens": 64, "reasoning": {"effort": "low"}}
        headers = {"Authorization": "Bearer " + key}
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read(400 if kind == "antigravity" else None)
    except Exception:
        return False
    if kind == "antigravity":
        return b'"candidates"' in raw
    data = json.loads(raw)
    if data.get("error"):
        return False
    if kind == "claude-code":
        return data.get("type") == "message"
    return data.get("status") in ("completed", "incomplete")


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def update_settings(candidates, results):
    """extra_models for the harnesses that have them. True when it changed."""
    settings = json.load(open(SETTINGS))
    harnesses = settings.setdefault("harnesses", {})
    changed = False
    for kind, models in candidates.items():
        if kind == "antigravity":
            continue
        live = [m for m in models if results.get((kind, m))]
        if kind == "codex":
            extra = [dict({"id": m}, **CODEX_EFFORTS) for m in live]
        else:
            extra = live
        if harnesses.setdefault(kind, {}).get("extra_models") != extra:
            harnesses[kind]["extra_models"] = extra
            changed = True
    if changed:
        write_json(SETTINGS, settings)
    return changed


def update_antigravity(candidates, results, cfg):
    """The custom models of the Antigravity CLI. True when they changed."""
    models = candidates.get("antigravity")
    if not models or not os.path.exists(ANTIGRAVITY_SETTINGS):
        return False
    base = (cfg.get("gemini_base_url") or cfg["base_url"] + "/gemini").split("://", 1)[-1]
    labels = cfg.get("antigravity_labels") or {}
    ag = json.load(open(ANTIGRAVITY_SETTINGS))
    custom = {}
    live_labels = []
    for name in models:
        model = name.lstrip("!")
        label = labels.get(model, model)
        if not name.startswith("!") and results.get(("antigravity", model)):
            live_labels.append(label)
        else:
            label += " [offline]"
        custom["mp-" + model] = {"modelName": "gemini-api://" + base + "/models/" + model,
                                 "label": label}
    cfg_models = ag.setdefault("customModelsConfig", {})
    if cfg_models.get("customModels") == custom and ag.get("model") in live_labels:
        return False
    cfg_models["customModels"] = custom
    if ag.get("model") not in live_labels and live_labels:
        ag["model"] = live_labels[0]  # the file's default went offline
    write_json(ANTIGRAVITY_SETTINGS, ag)
    return True


def main():
    cfg = json.load(open(CONFIG))
    candidates = cfg["models"]
    key = api_key(cfg)
    futures = {}
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for kind, models in candidates.items():
            for model in models:
                if model.startswith("!"):
                    continue
                futures[pool.submit(probe, kind, model, key, cfg)] = (kind, model)
        for f in concurrent.futures.as_completed(futures):
            results[futures[f]] = f.result()

    changed = update_settings(candidates, results)
    changed = update_antigravity(candidates, results, cfg) or changed

    probed = [(k, m) for k, ms in candidates.items() for m in ms if not m.startswith("!")]
    dead = [k + ":" + m for k, m in probed if not results.get((k, m))]
    msg = "live: %d/%d" % (len(probed) - len(dead), len(probed))
    if dead:
        msg += "; dead: " + ", ".join(dead)
    if changed:
        msg += "; settings updated"
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
