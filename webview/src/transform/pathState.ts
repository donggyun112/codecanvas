/**
 * 3-way path classification for nodes and edges.
 *
 * When a runtime trace exists, every node/edge falls into one of:
 *   - verified:     Executed at runtime (runtime_hit === true)
 *   - unverified:   Found by static analysis but NOT executed
 *   - runtime-only: Only discovered at runtime (confidence === 'runtime')
 *
 * Without a trace, everything is 'possible' (static analysis only).
 */

export type PathState = 'verified' | 'unverified' | 'runtime-only' | 'possible';

export function computeNodePathState(
  hasTrace: boolean,
  runtimeHit: boolean | undefined,
  confidence: string | undefined,
): PathState {
  if (!hasTrace) return 'possible';
  if (confidence === 'runtime') return 'runtime-only';
  return runtimeHit ? 'verified' : 'unverified';
}

export function computeEdgePathState(
  hasTrace: boolean,
  runtimeHit: boolean | undefined,
  confidence: string | undefined,
): PathState {
  if (!hasTrace) return 'possible';
  if (confidence === 'runtime') return 'runtime-only';
  return runtimeHit ? 'verified' : 'unverified';
}
