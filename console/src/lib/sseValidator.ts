/**
 * Hand-rolled SSE envelope validator.
 *
 * Why hand-rolled: `zod` was rejected by the Cybersec dependency approval
 * process (see `.agents/cybersecurity.md`). Every third-party runtime
 * dependency is a supply-chain attack surface; a 120-line validator with
 * zero deps is the right trade-off for the narrow schema we actually emit.
 *
 * Ground truth for the envelope shape is the Postgres NOTIFY payload built
 * in `src/codeatelier_governance/console/triggers.sql` (notify_governance_event):
 *
 *     json_build_object(
 *       'event_id',   NEW.event_id,
 *       'agent_id',   NEW.agent_id,
 *       'session_id', NEW.session_id,
 *       'kind',       NEW.kind,
 *       'chain_seq',  NEW.chain_seq,
 *       'created_at', NEW.created_at
 *     )
 *
 * Plus the optional `truncated: true` fallback written when the full
 * payload exceeds the 7 KB NOTIFY cap.
 *
 * Wiring: this helper is INTENTIONALLY NOT wired into `useEventStream.ts`
 * in v0.6. F2 already stabilised the SSE path via lazy hydration, and F1
 * touched the same hook in the same release. A runtime wiring change here
 * risks a three-way merge collision. The helper ships as an isolated
 * module and will be wired in v0.6.1 (or the next natural SSE touch-up).
 *
 * Threat model:
 *   - Prototype pollution: a JSON payload with `__proto__`, `constructor`,
 *     or `prototype` keys must be rejected (not copied through).
 *   - Type confusion: a string-valued `chain_seq` or a numeric `event_id`
 *     is a sign of a drifted backend and must be dropped.
 *   - Oversize fields: a single 100 KB `kind` value bypasses the NOTIFY
 *     guard via a direct REST injection path. Hard cap every string.
 *   - Timestamp parsing: `created_at` must round-trip through `Date.parse`.
 *
 * API:
 *   validateSseEnvelope(raw: unknown): StreamEvent | null
 *
 * Returns the validated envelope on success, `null` on any failure.
 * Callers SHOULD drop the event and log at debug level on null.
 */

const MAX_STR_LEN = 4096;
const FORBIDDEN_KEYS = new Set(["__proto__", "constructor", "prototype"]);

export interface StreamEvent {
  event_id: string;
  agent_id: string;
  session_id: string | null;
  kind: string;
  chain_seq: number;
  created_at: string;
  truncated?: true;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  if (v === null || typeof v !== "object") return false;
  if (Array.isArray(v)) return false;
  const proto = Object.getPrototypeOf(v);
  return proto === Object.prototype || proto === null;
}

function hasForbiddenKey(obj: Record<string, unknown>): boolean {
  for (const k of Object.keys(obj)) {
    if (FORBIDDEN_KEYS.has(k)) return true;
  }
  // Own-property check on the prototype-pollution sentinel too:
  // `Object.keys` skips non-enumerable props, but `hasOwnProperty` catches
  // a manually planted `__proto__` assignment on a plain object.
  if (Object.prototype.hasOwnProperty.call(obj, "__proto__")) return true;
  return false;
}

function isBoundedString(v: unknown): v is string {
  return typeof v === "string" && v.length > 0 && v.length <= MAX_STR_LEN;
}

function isFiniteInteger(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v) && Number.isInteger(v);
}

function isParseableTimestamp(v: unknown): v is string {
  if (!isBoundedString(v)) return false;
  const t = Date.parse(v);
  return Number.isFinite(t);
}

/**
 * Validate a raw SSE envelope parsed from `event.data`. Returns a typed
 * `StreamEvent` on success or `null` on any validation failure.
 */
export function validateSseEnvelope(raw: unknown): StreamEvent | null {
  if (!isPlainObject(raw)) return null;
  if (hasForbiddenKey(raw)) return null;

  const {
    event_id,
    agent_id,
    session_id,
    kind,
    chain_seq,
    created_at,
    truncated,
  } = raw;

  if (!isBoundedString(event_id)) return null;
  if (!isBoundedString(agent_id)) return null;
  if (!isBoundedString(kind)) return null;
  if (!isParseableTimestamp(created_at)) return null;
  if (!isFiniteInteger(chain_seq)) return null;
  if (chain_seq < 0) return null;

  // session_id is optional on the fallback (truncated) envelope.
  let sessionOut: string | null = null;
  if (session_id !== undefined && session_id !== null) {
    if (!isBoundedString(session_id)) return null;
    sessionOut = session_id;
  }

  const out: StreamEvent = {
    event_id,
    agent_id,
    session_id: sessionOut,
    kind,
    chain_seq,
    created_at,
  };
  if (truncated === true) out.truncated = true;
  return out;
}
