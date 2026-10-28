import { lazy, Suspense } from "react";
import { Spinner } from "./ui";

const LazyCode = lazy(() => import("./Editors").then((m) => ({ default: m.CodeEditor })));
const LazyDiff = lazy(() => import("./Editors").then((m) => ({ default: m.DiffEditor })));

const fallback = (
  <div className="empty-state">
    <Spinner />
  </div>
);

export function CodeEditor(props: React.ComponentProps<typeof LazyCode>) {
  return (
    <Suspense fallback={fallback}>
      <LazyCode {...props} />
    </Suspense>
  );
}

export function DiffEditor(props: React.ComponentProps<typeof LazyDiff>) {
  return (
    <Suspense fallback={fallback}>
      <LazyDiff {...props} />
    </Suspense>
  );
}
