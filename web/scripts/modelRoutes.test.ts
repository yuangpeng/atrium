import assert from "node:assert/strict";
import test from "node:test";
import { modelIdentity, modelRoutes } from "../src/chat/modelRoutes.ts";
import type { Harness } from "../src/api.ts";

const gpt = "gpt-fixture-with-a-long-model-name";
const claude = "claude-fixture-with-a-long-model-name";
const gemini = "gemini-fixture-with-a-long-model-name";
function harness(id: string, ids: string[], available = true): Harness {
  return { id, label: id, available, permission_modes: [], efforts: [], models: ids.map((id) => ({ id, label: id })) };
}

test("all three models remain reachable from Codex; duplicate runtimes do not duplicate GPT", () => {
  const routes = modelRoutes([
    harness("claude-code", ["", claude]), harness("pi", ["", "relay/" + gpt]),
    harness("antigravity", ["", gemini]), harness("dsh", ["relay/" + gpt]), harness("codex", ["", gpt]),
  ], "codex");
  assert.deepEqual(routes.map((r) => [r.harness.id, r.model.id]), [["codex", gpt], ["claude-code", claude], ["antigravity", gemini]]);
});

test("an unavailable preferred runtime cannot receive a selection", () => {
  const routes = modelRoutes([harness("codex", [gpt], false), harness("pi", ["relay/" + gpt])], "codex");
  assert.deepEqual(routes.map((r) => [r.harness.id, r.model.id]), [["pi", "relay/" + gpt]]);
});

test("provider-qualified IDs are preserved for the runtime request", () => {
  const route = modelRoutes([harness("dsh", ["proxy/team/model"])], "codex")[0]!;
  assert.equal(route.model.id, "proxy/team/model");
  assert.equal(modelIdentity(route.harness, route.model.id), "team/model");
});

test("different routing tags and native model namespaces stay distinct", () => {
  const routes = modelRoutes([harness("codex", ["team/model", "model", "model:route-a", "model:route-b"])], "codex");
  assert.equal(routes.length, 4);
});

test("the catalog is not mutated when choosing a preferred runtime", () => {
  const input = [harness("claude-code", [claude]), harness("codex", [gpt])];
  const before = structuredClone(input);
  modelRoutes(input, "codex");
  assert.deepEqual(input, before);
});
