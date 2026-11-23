import type { Harness, HarnessModel } from "../api";

export interface ModelRoute {
  harness: Harness;
  model: HarnessModel;
}

/** Pi and DSH qualify a model with their local provider name. Keep that name for calls,
 * but offer the same underlying model only once in the shared picker. */
export function modelIdentity(harness: Harness, model: string): string {
  return ["pi", "dsh"].includes(harness.id) && model.includes("/") ? model.slice(model.indexOf("/") + 1) : model;
}

export function modelRoutes(harnesses: Harness[], preferredHarness: string): ModelRoute[] {
  const ordered = [...harnesses].sort((a, b) => Number(b.id === preferredHarness) - Number(a.id === preferredHarness));
  const seen = new Set<string>();
  const routes: ModelRoute[] = [];
  for (const harness of ordered) {
    if (!harness.available) continue;
    for (const model of harness.models) {
      const identity = modelIdentity(harness, model.id);
      if (!identity || seen.has(identity)) continue;
      seen.add(identity);
      routes.push({ harness, model });
    }
  }
  return routes;
}
