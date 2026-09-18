// tianji-state.ts — Command Code native event bridge for the shared Tianji ledger.
//
// Responsibilities (host primitives only):
//   1. decode Command Code subagent events into the shared ledger envelope;
//   2. claim an invocation through the shared registry process boundary;
//   3. append to .tianji/state.jsonl under the shared ledger lock;
//   4. drive the host footer from raw dispatch facts, never a health verdict.
//
// This file never decides task state, acceptance or compensation: those live in
// the shared core. It never reads the registry's internal files: identity comes
// only from the shared claim, over a JSON stdin/stdout boundary. It fails open —
// a mod error must not break the host.

import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { spawnSync } from 'node:child_process';

export const HOST = 'cmdc';
export const SCHEMA_VERSION = 2;

// The claim is a short, bounded call: if it does not answer quickly the event
// stays unbound rather than committing an identity late and out of order.
export const CLAIM_TIMEOUT_MS = 5_000;

const DETAIL_WHITELIST = new Set([
  'tokensUsed', 'toolName', 'toolInput', 'subagentType',
  'description', 'showOutput', 'reason', 'error', 'receipt',
  // The mod's own running call count, added to progress rows below. The call
  // meters read it back from the ledger (`toolCalls`, cumulative, max per
  // invocation) and no host payload carries it.
  'toolCalls',
  // The measured duration, stamped by the mod onto the receipt row. A worker's
  // own estimate of its wall clock is prose: it has no clock that reads the
  // host's, and one reported "~17 分钟" for a dispatch this measured at 3m52s.
  'durationMs', 'durationSource',
]);

// Per-key caps: a tool input is a line, but a worker's final receipt is the
// deliverable and must not be truncated to a snippet.
const MAX_CHARS_BY_KEY: Record<string, number> = { receipt: 8000 };

const SECRET_RES = [
  /sk-[A-Za-z0-9_-]{8,}/gi,
  /Bearer\s+[A-Za-z0-9._-]{6,}/gi,
  /(?:api[_-]?key|token|secret|password|authorization)["'\s:=]+[A-Za-z0-9._~+/=-]{8,}/gi,
  /ghp_[A-Za-z0-9]{20,}/gi,
  /xox[baprs]-[A-Za-z0-9-]{10,}/gi,
  /AIza[0-9A-Za-z_-]{20,}/g,
  /hf_[A-Za-z0-9]{20,}/gi,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
];

const MAX_DETAIL_CHARS = 500;
const MAX_RECEIPT_CHARS = 8000;
// The Python sink's dedup window; keep them equal so both writers agree on how
// far back a duplicate is recognized.
const LEDGER_SCAN_LIMIT = 5000;

export function maskSecrets(value: unknown): unknown {
  if (typeof value === 'string') {
    let text = value;
    for (const pattern of SECRET_RES) text = text.replace(pattern, '[MASKED]');
    return text;
  }
  if (Array.isArray(value)) return value.map(maskSecrets);
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      out[key] = maskSecrets(item);
    }
    return out;
  }
  return value;
}

export function sanitizeDetail(payload: unknown): Record<string, unknown> {
  if (!payload || typeof payload !== 'object') return {};
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(payload as Record<string, unknown>)) {
    if (!DETAIL_WHITELIST.has(key)) continue;
    let masked = maskSecrets(value);
    if (key === 'description') masked = stripClaimToken(masked);
    if (masked !== null && typeof masked === 'object') masked = JSON.stringify(masked);
    const cap = MAX_CHARS_BY_KEY[key] ?? MAX_DETAIL_CHARS;
    out[key] = typeof masked === 'string' && masked.length > cap
      ? masked.slice(0, cap)
      : masked;
  }
  return out;
}

export function truncateReceipt(value: unknown): string {
  const maskSecretsValue = maskSecrets(value);
  const text = typeof maskSecretsValue === 'string'
    ? maskSecretsValue
    : JSON.stringify(maskSecretsValue ?? '');
  return text.slice(0, MAX_RECEIPT_CHARS);
}

// ---------------------------------------------------------------------------
// Claim marker — the one channel a dispatch description offers.
// ---------------------------------------------------------------------------

/** Anchored at the end, one marker, the encoding the shared protocol mints. */
export const CLAIM_MARKER_RE = /\[TJ:([A-Za-z0-9_-]{22})\]\s*$/;

export function extractClaimToken(description: unknown): string | null {
  if (typeof description !== 'string') return null;
  const match = CLAIM_MARKER_RE.exec(description);
  return match ? match[1] : null;
}

/** The marker is a capability: it must never reach the ledger. */
export function stripClaimToken(description: unknown): string {
  if (typeof description !== 'string') return description === null || description === undefined ? '' : String(description);
  return description.replace(CLAIM_MARKER_RE, '').trim();
}

// ---------------------------------------------------------------------------
// Shared ledger lock — same file, payload and staleness rule as lock_protocol.
// ---------------------------------------------------------------------------

const LOCK_TIMEOUT_MS = 5_000;
const LOCK_LEASE_SECONDS = 60;
const UNREADABLE_GRACE_MS = 1_000;
const UNLINK_ATTEMPTS = 200;
const UNLINK_RETRY_MS = 5;

interface LockPayload {
  pid: number;
  host: string;
  acquired_at: number;
  lease_expires_at: number;
}

export function ledgerLockPath(workspace: string): string {
  return path.join(workspace, '.tianji', 'state.lock');
}

function sleepSync(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function pidAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM means it exists but is not ours; only ESRCH proves it is gone.
    return (error as NodeJS.ErrnoException).code === 'EPERM';
  }
}

function readLockPayload(target: string): LockPayload | null {
  try {
    const value = JSON.parse(fs.readFileSync(target, 'utf-8'));
    return value && typeof value === 'object' ? value as LockPayload : null;
  } catch {
    return null;
  }
}

function lockIsStale(info: LockPayload | null, nowMs: number): boolean {
  if (!info || typeof info.pid !== 'number') return true;
  if (info.host && info.host === os.hostname() && !pidAlive(info.pid)) return true;
  if (typeof info.lease_expires_at === 'number' && nowMs / 1000 > info.lease_expires_at) return true;
  return false;
}

function unlinkWithRetry(target: string): void {
  for (let attempt = 0; attempt < UNLINK_ATTEMPTS; attempt++) {
    try {
      fs.unlinkSync(target);
      return;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return;
      sleepSync(UNLINK_RETRY_MS);
    }
  }
}

function reapIfStale(target: string): void {
  const info = readLockPayload(target);
  if (!info) {
    try {
      if (Date.now() - fs.statSync(target).mtimeMs < UNREADABLE_GRACE_MS) return;
    } catch {
      return;
    }
  }
  if (!lockIsStale(info, Date.now())) return;
  unlinkWithRetry(target);
}

/**
 * Run `fn` while holding the shared ledger lock.
 *
 * Returns null when the lock could not be taken, so a caller never writes
 * unprotected: an uncontended append is the only append worth making.
 */
export function withLedgerLock<T>(workspace: string, fn: () => T): T | null {
  const target = ledgerLockPath(workspace);
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true });
  } catch {
    return null;
  }
  const deadline = Date.now() + LOCK_TIMEOUT_MS;
  for (;;) {
    try {
      const handle = fs.openSync(target, 'wx');
      try {
        const payload: LockPayload = {
          pid: process.pid,
          host: os.hostname(),
          acquired_at: Date.now() / 1000,
          lease_expires_at: Date.now() / 1000 + LOCK_LEASE_SECONDS,
        };
        fs.writeSync(handle, JSON.stringify(payload));
      } finally {
        fs.closeSync(handle);
      }
      break;
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== 'EEXIST' && code !== 'EPERM') return null;
      reapIfStale(target);
      if (Date.now() >= deadline) return null;
      sleepSync(UNLINK_RETRY_MS);
    }
  }
  try {
    return fn();
  } finally {
    unlinkWithRetry(target);
  }
}

// ---------------------------------------------------------------------------
// Claim bridge — the only way this file learns an identity.
// ---------------------------------------------------------------------------

export interface ClaimedIdentity {
  run_id: string;
  task_id: string;
  attempt: number;
  invocation_id: string;
  tool_call_id: string;
  // The binding the dispatcher recorded. Present so the ledger reports the
  // dispatch-time fact rather than whatever the role file says at event time.
  model?: string;
  model_source?: string;
}

export interface ClaimRequest {
  workspace: string;
  host: string;
  sessionId: string;
  token: string;
  toolCallId: string;
  role?: string;
}

export interface ClaimOutcome {
  ok: boolean;
  identity?: ClaimedIdentity;
  reused?: boolean;
  error?: string;
}

/** The installer records where the shared core and its interpreter live. */
export const RUNTIME_LOCATOR = 'tianji-runtime.json';

export interface RuntimeLocator {
  interpreter: string;
  registry: string;
  scriptsDir: string;
}

export function cmdcHome(env: NodeJS.ProcessEnv = process.env): string {
  return env.CMDC_HOME || path.join(os.homedir(), '.commandcode');
}

export function readRuntimeLocator(home = cmdcHome()): RuntimeLocator | null {
  try {
    const value = JSON.parse(fs.readFileSync(path.join(home, RUNTIME_LOCATOR), 'utf-8'));
    if (value && typeof value.interpreter === 'string' && typeof value.registry === 'string') {
      return {
        interpreter: value.interpreter,
        registry: value.registry,
        // A locator written before the scripts directory was recorded still
        // resolves: the registry lives in it.
        scriptsDir: typeof value.scripts_dir === 'string' && value.scripts_dir
          ? value.scripts_dir : path.dirname(value.registry),
      };
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * This session's spend meter, asked of the script that defines it.
 *
 * The Command Code footer is drawn here, in the mod; the Kimi footer is drawn by
 * statusline.py. The two must not disagree about what a session spent, so the
 * reading of the ledger (spend) and of the registry (ceilings) stays in that one
 * file and this only carries its answer into the line. Unreadable means empty:
 * a meter that invents a number is worse than a meter that says nothing.
 */
export function meterLine(
  workspace: string, sessionId: string,
  deps: { locator?: RuntimeLocator; home?: string } = {},
): string {
  const locator = deps.locator ?? readRuntimeLocator(deps.home);
  if (!locator || !sessionId) return '';
  try {
    const result = spawnSync(
      locator.interpreter,
      [path.join(locator.scriptsDir, 'statusline.py'), '--meter'],
      {
        input: JSON.stringify({ cwd: workspace, sessionId }),
        encoding: 'utf-8',
        windowsHide: true,
      },
    );
    if (result.status !== 0) return '';
    return String(result.stdout ?? '').trim().split('\n')[0].trim();
  } catch {
    return '';
  }
}

export function claimInvocation(
  request: ClaimRequest, options: { home?: string; locator?: RuntimeLocator | null } = {},
): ClaimOutcome {
  const locator = options.locator ?? readRuntimeLocator(options.home);
  if (!locator) return { ok: false, error: 'tianji runtime locator is not installed' };

  // spawnSync with an argument array, never a shell: the workspace, token and
  // call id are data, and must not be re-parsed as command text.
  const result = spawnSync(locator.interpreter, [locator.registry, 'claim'], {
    input: JSON.stringify({
      workspace: request.workspace,
      host: request.host,
      session_id: request.sessionId,
      token: request.token,
      tool_call_id: request.toolCallId,
      role: request.role ?? '',
    }),
    encoding: 'utf-8',
    timeout: CLAIM_TIMEOUT_MS,
    windowsHide: true,
  });

  if (result.error) return { ok: false, error: String(result.error.message ?? result.error) };
  const stdout = typeof result.stdout === 'string' ? result.stdout.trim() : '';
  if (result.status !== 0) {
    return { ok: false, error: parseClaimError(stdout) ?? `claim exited ${result.status}` };
  }
  try {
    const payload = JSON.parse(stdout);
    if (!payload || payload.ok !== true) {
      return { ok: false, error: String(payload?.error ?? 'claim did not confirm') };
    }
    return {
      ok: true,
      reused: payload.reused === true,
      identity: {
        run_id: String(payload.run_id),
        task_id: String(payload.task_id),
        attempt: Number(payload.attempt),
        invocation_id: String(payload.invocation_id),
        tool_call_id: String(payload.tool_call_id),
        model: String(payload.model ?? ''),
        model_source: String(payload.model_source ?? ''),
      },
    };
  } catch {
    return { ok: false, error: 'claim returned unreadable output' };
  }
}

function parseClaimError(stdout: string): string | null {
  try {
    const payload = JSON.parse(stdout);
    return typeof payload?.error === 'string' ? payload.error : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Ledger and diagnostics
// ---------------------------------------------------------------------------

/**
 * What one in-flight dispatch has shown us so far.
 *
 * Raw observations only. Whether a worker is healthy, slow or stuck is a
 * judgement over shared state, and it belongs to the shared layer: this file
 * must not invent a verdict (or an alert event) from its own thresholds.
 */
export interface WorkerState {
  role: string;
  model: string;
  modelSource: string;
  taskId: string;
  toolCallId: string;
  startTs: string;
  lastAction: string;
  lastActivityMs: number;
  /**
   * Child tool calls this seat has been seen making.
   *
   * Turns would be the number that matters -- they are what the host's cap
   * counts -- but this host does not offer them: its sub-agent events pass
   * through a translator that forwards only `tool_queued` (the nested run's
   * turn boundaries are dropped, not relayed), and `subagent_stop` carries only
   * `toolCallId`, `subagentType` and `tokensUsed`. What is attributable and
   * always available is the per-call progress event, so that is what the footer
   * shows -- labelled as steps, never as turns.
   */
  steps: number;
}

/**
 * What a seat is doing right now: the tool, and what it was pointed at.
 *
 * The host's own sub-agent view carries the same pair (`recentTools: {name,
 * input}`), and the name alone says only that a dispatch is alive -- "read_file"
 * does not tell a reader whether it is auditing the change or reading the rules.
 * A child's thinking and streamed text are consumed by the host's display and
 * never reach a mod, so its tool calls are the most current fact a footer can
 * honestly carry.
 */
export function actionLabel(toolName: unknown, toolInput: unknown): string {
  const tool = String(toolName ?? '').trim();
  const text = String(toolInput ?? '').replace(/\s+/g, ' ').trim();
  if (!tool) return text;
  if (!text) return tool;
  // A shell's input is a command line, not a name. One real dispatch put
  // `cd ...scratchpad && python -c "prin...` in this field: the thirty
  // characters a seat could afford went to a temporary path, and the label
  // answered nothing. A command line is reduced to its head first, so the same
  // budget buys an answer instead of a prefix.
  if (SHELL_TOOLS.has(tool)) {
    const head = commandHead(text);
    if (head) {
      return head.target
        ? `${clip(head.tool, ACTION_TOOL_CHARS)} ${clip(head.target, ACTION_TARGET_CHARS)}`
        : clip(head.tool, ACTION_TOOL_CHARS);
    }
  }
  // The last path segment is the part worth the room: the directory the work
  // happens in is already visible in the line. It is clipped on its own budget
  // rather than inside the whole label, or a long tool name would eat the part
  // that says what the dispatch is actually doing.
  const segments = text.split(/[\\/]/).filter(Boolean);
  const aimed = segments.length > 1 ? segments[segments.length - 1] : text;
  return `${clip(tool, ACTION_TOOL_CHARS)} ${clip(aimed, ACTION_TARGET_CHARS)}`;
}

/** Tools whose input is a whole command line rather than a name. */
export const SHELL_TOOLS = new Set([
  'shell_command', 'shell', 'bash', 'sh', 'zsh', 'powershell', 'pwsh', 'cmd',
  'run_command', 'run_terminal_cmd', 'exec', 'terminal',
]);

/** The last segment of a path or a command word: the piece a line has room for. */
function lastSegment(text: string): string {
  const value = String(text ?? '').replace(/^["']|["']$/g, '');
  const segments = value.split(/[\\/]/).filter(Boolean);
  return segments.length ? segments[segments.length - 1] : value;
}

/**
 * A command line's head: the program it runs, and the script it points at.
 *
 * A chain is read as its first real command, because that is what was launched;
 * `cd <dir>` is navigation the seat's own directory already implies; a flag is
 * not a name; and an inline expression (`-c`, `-e`) carries program text with no
 * name in it to show at all. What is left is one program and one thing it was
 * pointed at -- enough to say what the seat is doing without spending the budget
 * on a temporary path.
 */
function commandHead(command: string): { tool: string; target: string } | null {
  const segments = command.split(/\s*(?:&&|\|\||;|\|)\s*/).map(part => part.trim()).filter(Boolean);
  for (const segment of segments) {
    const tokens = segment.split(' ').filter(Boolean);
    if (!tokens.length) continue;
    const tool = lastSegment(tokens[0]);
    const lower = tool.toLowerCase();
    if (lower === 'cd' || lower === 'set-location' || lower === 'pushd') continue;
    if (['-c', '-e', '--eval'].includes(tokens[1] ?? '')) return { tool, target: '' };
    const positioned = tokens.slice(1).filter(token => !/^[-/]/.test(token));
    return { tool, target: positioned.length ? lastSegment(positioned[0]) : '' };
  }
  return null;
}

/** How much of "what it was pointed at" a seat keeps: a file name, not a path. */
export const ACTION_TARGET_CHARS = 30;

/** How much of the tool's name a seat keeps; real names are short. */
export const ACTION_TOOL_CHARS = 16;

/**
 * How much of the whole action a seat keeps -- derived, never eyeballed.
 *
 * The first cut clipped the whole label to 20, which left eleven characters for
 * the file: an ordinary name came out as "read_file RUNTIME-PRO…" and told the
 * reader nothing. Two budgets replaced it, and the seat's own limit is their sum
 * so it can never quietly eat part of what they were sized to show.
 */
export const ACTION_CHARS = ACTION_TOOL_CHARS + 1 + ACTION_TARGET_CHARS;

/** Keep a footer field inside its budget without dropping the field. */
export function clip(text: string, max: number): string {
  const value = String(text ?? '').trim();
  return value.length > max ? `${value.slice(0, Math.max(1, max - 1))}…` : value;
}

/**
 * The model in the width a footer has.
 *
 * A full id such as ``deepseek/deepseek-v4.1-flash`` is mostly vendor prefix;
 * the part after the last slash is what a human recognises.
 */
export function shortModel(model: string): string {
  const value = String(model ?? '').trim();
  if (!value) return '?';
  const tail = value.includes('/') ? value.slice(value.lastIndexOf('/') + 1) : value;
  return clip(tail, 20);
}

/** How long a dispatch has been running, not how long it has been quiet. */
export function shortDuration(startMs: number, nowMs: number): string {
  const seconds = Math.max(0, Math.round((nowMs - startMs) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`;
}

// ---------------------------------------------------------------------------
// The footer's whole line: one budget, and every field derived from it
// ---------------------------------------------------------------------------
//
// The footer is a single line and the host cuts it from the right, so a line
// that is too long does not look wrong -- it loses its last seats. That is how
// two parallel seats became one, and how the `+N` that counted the rest was
// eaten along with them. Everything below is therefore derived from one width,
// so the line is already legal by the time the host sees it.
//
// FOOTER_WIDTH is the one number here that is a host fact rather than a
// derivation: how many cells the status area lets a line fill. 118 is the
// planning figure; it was *not* re-measured against a live footer in this
// change, so it stays the single place to correct if a real footer turns out to
// cut somewhere else -- every other number is derived from it.
export const FOOTER_WIDTH = 118;

/** A seat's field budgets: the caps, not the sizes. The sizes are computed. */
export const SEAT_ROLE_CHARS = 3;
export const SEAT_DURATION_CHARS = 5;
export const SEAT_STEPS_CHARS = 4;
/**
 * A tool keeps its name: the longest one this host reports is `shell_command`
 * (13) and `read_file` (9) is what most steps are. The first budget here was
 * eight, which cut both -- and a tool cut mid-word says nothing about what the
 * seat is doing, which is the only reason it is on the line at all.
 */
export const SEAT_TOOL_CHARS = 13;
/**
 * Below this a tool's name stops naming the tool: seats are shed instead.
 *
 * Nine is `read_file`. Under it the line keeps showing a seat nobody can read,
 * while shedding a seat at least says how many are hidden (`+N`), and the seats
 * kept are the longest-running ones -- which is where a stuck seat lives.
 */
export const SEAT_TOOL_MIN_CHARS = 9;
/** The rungs "what it was pointed at" gives up, in order, before anything else. */
export const SEAT_TARGET_RUNGS = [30, 20, 12, 8];
/** Four seats fit; a fifth is where the line starts saying `+N`. */
export const SEAT_MAX_SEATS = 4;
/** Silence, not a verdict: a seat quiet this long is marked `!`, then `!!`. */
export const SEAT_STUCK_WARN_MS = 60_000;
export const SEAT_STUCK_BAD_MS = 300_000;
/** The task number's share of the one-seat line, which still shows it. */
export const TASK_CHARS = 12;
export const SEAT_LEAD = '▸ ';
export const SEAT_SEPARATOR = ' │ ';

/**
 * The footer is styled, and the host prints the escapes verbatim: its docs say
 * `cmd.ui.setStatus` takes text "printed verbatim (style it with ansi)".
 *
 * Colour is applied where a field is assembled and never counted -- `displayWidth`
 * strips it first, so a budget in cells cannot be spent by an escape sequence.
 */
export const ANSI_PATTERN = /\u001b\[[0-9;]*m/g;
export const C_DIM = '\u001b[38;5;245m';
export const C_WORKER = '\u001b[38;5;114m';
export const C_VERIFIER = '\u001b[38;5;111m';
export const C_REFEREE = '\u001b[38;5;180m';
export const C_QUIET = '\u001b[38;5;179m';
export const C_BAD = '\u001b[1;38;5;196m';
/** The clock: a number the reader is meant to act on, so it is not background. */
export const C_TIME = '\u001b[38;5;215m';
/** How much work has gone by: a second opinion on the clock. */
export const C_STEPS = '\u001b[38;5;146m';
/** The tool's name -- the subject of "what is this seat doing right now". */
export const C_TOOL = '\u001b[38;5;81m';
export const C_RESET = '\u001b[0m';

/** The colour a role's name is drawn in: one per kind, dim for anything else. */
export function roleColor(role: unknown): string {
  const name = String(role ?? '').replace(/^tianji-/, '');
  if (name === 'worker') return C_WORKER;
  if (name === 'verifier') return C_VERIFIER;
  if (name === 'referee') return C_REFEREE;
  return C_DIM;
}

/**
 * How wide text is on the footer: its CJK parts take two cells.
 *
 * The budget is in cells, and `12步` is four of them rather than three. Counting
 * characters instead would have made every seat four cells wider than the number
 * the budget was derived from, which is how a line ends up cut by the host again.
 * Escape sequences are not cells at all: they are what the reader never sees.
 */
export function displayWidth(text: string): number {
  let width = 0;
  for (const char of String(text ?? '').replace(ANSI_PATTERN, '')) {
    width += isWideCode(char.codePointAt(0) ?? 0) ? 2 : 1;
  }
  return width;
}

function isWideCode(code: number): boolean {
  return (
    (code >= 0x1100 && code <= 0x115f)
    || (code >= 0x2e80 && code <= 0xa4cf)
    || (code >= 0xac00 && code <= 0xd7a3)
    || (code >= 0xf900 && code <= 0xfaff)
    || (code >= 0xfe30 && code <= 0xfe4f)
    || (code >= 0xff00 && code <= 0xff60)
    || (code >= 0xffe0 && code <= 0xffe6)
    || (code >= 0x1f300 && code <= 0x1faff)
    || (code >= 0x20000 && code <= 0x3fffd)
  );
}

/**
 * Keep a whole line inside a cell budget, whatever it holds.
 *
 * This is the last resort -- a line that has already given up every rung -- so
 * it measures and returns plain text: an escape cut in half would be worse than
 * losing the colour on the one line that had to be cut anyway.
 */
function clipWidth(text: string, max: number): string {
  const plain = String(text ?? '').replace(ANSI_PATTERN, '');
  if (displayWidth(plain) <= max) return text;
  let out = '';
  for (const char of plain) {
    if (displayWidth(out + char) > Math.max(1, max - 1)) break;
    out += char;
  }
  return `${out}…`;
}

/** When a seat started: the line's order is pinned to it, so it cannot jump. */
function seatStartedAt(worker: WorkerState): number {
  const parsed = Date.parse(String(worker?.startTs ?? ''));
  return Number.isFinite(parsed) ? parsed : 0;
}

/** How long a seat has run, in five cells: `45s`, `12:01`, `1h05`. */
export function seatElapsed(startMs: number, nowMs: number): string {
  const seconds = Math.max(0, Math.round((nowMs - startMs) / 1000));
  let text: string;
  if (seconds < 60) text = `${seconds}s`;
  else if (seconds < 3600) text = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  else text = `${Math.floor(seconds / 3600)}h${String(Math.floor((seconds % 3600) / 60)).padStart(2, '0')}`;
  return clip(text, SEAT_DURATION_CHARS);
}

/** Steps in four cells: a seat that has been through a thousand says `2k步`. */
export function seatSteps(steps: unknown): string {
  const value = Math.max(0, Math.floor(Number(steps) || 0));
  return clip(value >= 1000 ? `${Math.floor(value / 1000)}k步` : `${value}步`, SEAT_STEPS_CHARS);
}

/** A seat that has gone quiet: a mark, never a verdict (the shared layer judges). */
export function seatStuckMark(lastActivityMs: unknown, nowMs: number): string {
  const last = Number(lastActivityMs);
  const quiet = Number.isFinite(last) ? nowMs - last : 0;
  if (quiet > SEAT_STUCK_BAD_MS) return '!!';
  if (quiet > SEAT_STUCK_WARN_MS) return '!';
  return '';
}

/** One seat: role, how long, how many steps, its tool, and what it points at. */
function seatBlock(
  worker: WorkerState, nowMs: number, targetChars: number, toolChars: number,
): string {
  // A role's three cells are the name's first three letters rather than a clip:
  // an ellipsis would spend a cell that the name itself can use, and the roles
  // this mod dispatches (`worker`, `verifier`, `referee`) stay apart at three.
  const role = String(worker?.role ?? '').replace(/^tianji-/, '').slice(0, SEAT_ROLE_CHARS);
  const action = String(worker?.lastAction ?? '').trim();
  const split = action.indexOf(' ');
  // `lastAction` is already a "tool target" label; the two parts are read back
  // apart because a seat re-budgets both -- the tool's name may be narrower here
  // than in the one-seat line, and the target is re-cut on every rung.
  const tool = action ? (split > 0 ? action.slice(0, split) : action) : '-';
  const aimed = split > 0 ? action.slice(split + 1) : '';
  const fields = [
    `${roleColor(worker?.role)}${role}${C_RESET}`,
    `${C_TIME}${seatElapsed(seatStartedAt(worker), nowMs)}${C_RESET}`,
    `${C_STEPS}${seatSteps(worker?.steps)}${C_RESET}`,
  ];
  // A seat with no tool budget still says a role, a clock and a step count: the
  // action is the last thing to go, because every running seat belongs on the
  // line and a seat hidden behind `+N` says nothing at all.
  if (toolChars > 0) fields.push(`${C_TOOL}${clip(tool, toolChars)}${C_RESET}`);
  if (targetChars > 0 && aimed && toolChars > 0) {
    fields.push(`${C_DIM}${clip(aimed, targetChars)}${C_RESET}`);
  }
  const mark = seatStuckMark(worker?.lastActivityMs, nowMs);
  // A quiet seat's mark is the one thing it can still say, so it gets a colour
  // of its own: amber for quiet, red-bold for stopped talking.
  if (mark) fields.push(`${mark === '!!' ? C_BAD : C_QUIET}${mark}${C_RESET}`);
  return fields.join(' ');
}

/**
 * The footer's whole line: the meter, then one block per running seat.
 *
 * Two or more seats show role, elapsed, steps, tool and target. The model and
 * the task number are the two fields that leave at that point: the model is
 * constant per role (a repeat of information), and the task number is the least
 * dense thing a line has when it is tight -- the board carries the full roster.
 * The order is by start time and does not change while the seats run: a line
 * whose seats swap places cannot be read twice.
 *
 * The line is cut here, to `width`, and never by the host: the reader gets every
 * seat that fits plus an honest `+N` for the rest, instead of a right-hand slice
 * that removes whoever happened to be listed last. One seat keeps the shape it
 * has always had, down to the byte.
 */
export function composeSeats(
  workers: WorkerState[], meter: string, width = FOOTER_WIDTH, nowMs = Date.now(),
): string {
  const reading = String(meter ?? '').trim();
  // The meter turns red on its own warning: it already ends a live overrun with
  // a `!`, and one colour a reader learns once beats a longer word on every line.
  const readingText = reading
    ? `${reading.endsWith('!') ? C_BAD : C_DIM}${reading}${C_RESET} `
    : '';
  const lead = `${C_DIM}tianji: ${C_RESET}${readingText}`;
  const leadMark = `${C_DIM}${SEAT_LEAD}${C_RESET}`;
  const separator = `${C_DIM}${SEAT_SEPARATOR}${C_RESET}`;
  const ordered = [...(workers ?? [])].sort((a, b) => seatStartedAt(a) - seatStartedAt(b));
  // Nothing running is not a seat with empty fields; it is the line the footer
  // has always shown at rest, and the meter is deliberately absent from it.
  if (!ordered.length) return `${C_DIM}tianji: 待命${C_RESET}`;

  if (ordered.length === 1) {
    const only = ordered[0];
    const started = Date.parse(String(only.startTs ?? '')) || nowMs;
    const fields = [
      `${roleColor(only.role)}${String(only.role ?? '').replace('tianji-', '')}@${shortModel(only.model)}${C_RESET}`,
      `${C_DIM}${clip(only.taskId || '-', TASK_CHARS)}${C_RESET}`,
      `${C_TOOL}${clip(only.lastAction || '-', ACTION_CHARS)}${C_RESET}`,
      `${C_TIME}${shortDuration(started, nowMs)}${C_RESET}`,
      `${C_STEPS}${only.steps}步${C_RESET}`,
    ];
    const line = `${lead}${fields.join(' ')}`;
    // A lone seat has room for its identity, so the model and the task stay.
    if (displayWidth(line) <= width) return line;
    // It does not fit even alone: give up the target, not the identity.
    const action = String(only.lastAction ?? '').trim();
    const split = action.indexOf(' ');
    const tool = split > 0 ? action.slice(0, split) : (action || '-');
    const aimed = split > 0 ? action.slice(split + 1) : '';
    for (const rung of [...SEAT_TARGET_RUNGS, 0]) {
      const label = aimed && rung > 0
        ? `${C_TOOL}${clip(tool, ACTION_TOOL_CHARS)}${C_RESET} ${C_DIM}${clip(aimed, rung)}${C_RESET}`
        : `${C_TOOL}${clip(tool, ACTION_TOOL_CHARS)}${C_RESET}`;
      const candidate = `${lead}${[fields[0], fields[1], label, fields[3], fields[4]].join(' ')}`;
      if (displayWidth(candidate) <= width) return candidate;
    }
    return clipWidth(line, width);
  }

  const leadWidth = displayWidth(`${lead}${SEAT_LEAD}`);
  let seats = ordered;
  let dropped = 0;
  for (;;) {
    const tail = dropped > 0 ? ` ${C_DIM}+${dropped}${C_RESET}` : '';
    const separators = displayWidth(SEAT_SEPARATOR) * (seats.length - 1);
    const perSeat = Math.floor((width - leadWidth - displayWidth(tail) - separators) / seats.length);
    let chosen: string[] | null = null;
    for (const rung of [...SEAT_TARGET_RUNGS, 0]) {
      const blocks = seats.map(seat => seatBlock(seat, nowMs, rung, SEAT_TOOL_CHARS));
      if (blocks.every(block => displayWidth(block) <= perSeat)) {
        chosen = blocks;
        break;
      }
    }
    if (!chosen) {
      // The tool's name is the last field to shrink: a name cut to three cells
      // still says what kind of work is happening, which is why the seat is on
      // the line at all -- while a seat the line drops says nothing at all.
      for (let toolChars = SEAT_TOOL_CHARS - 1; toolChars >= SEAT_TOOL_MIN_CHARS; toolChars--) {
        const blocks = seats.map(seat => seatBlock(seat, nowMs, 0, toolChars));
        if (blocks.every(block => displayWidth(block) <= perSeat)) {
          chosen = blocks;
          break;
        }
      }
    }
    if (!chosen) {
      // Dropping a seat is the very last resort. Every running seat belongs on
      // the line, so all of them give up the action before one of them gives up
      // its place: a reader still learns how many are running, how long each has
      // been at it, and which of them has gone quiet -- while `+N` hides a
      // worker entirely.
      const actionless = seats.map(seat => seatBlock(seat, nowMs, 0, 0));
      if (actionless.every(block => displayWidth(block) <= perSeat)) chosen = actionless;
    }
    if (chosen) return `${lead}${leadMark}${chosen.join(separator)}${tail}`;
    // Out of room inside a seat: hide seats rather than let the host cut the
    // line. The seats kept are the ones that have been running longest -- a
    // stuck seat is the oldest, and it is the one a reader most needs to see.
    if (seats.length > SEAT_MAX_SEATS) {
      dropped += seats.length - SEAT_MAX_SEATS;
      seats = seats.slice(0, SEAT_MAX_SEATS);
      continue;
    }
    if (seats.length > 1) {
      dropped += 1;
      seats = seats.slice(0, seats.length - 1);
      continue;
    }
    return clipWidth(
      `${lead}${leadMark}${seatBlock(seats[0], nowMs, 0, SEAT_TOOL_MIN_CHARS)}${tail}`, width,
    );
  }
}

/** The shared v2 envelope, and nothing else: extra fields are a schema error. */
export interface LedgerRecord {
  schema_version: number;
  event_id: string;
  event: string;
  host: string;
  run_id: string;
  session_id: string;
  task_id: string;
  attempt: number;
  invocation_id: string;
  correlation_id: string;
  agent: string;
  occurred_at: string;
  recorded_at: string;
  detail: Record<string, unknown>;
}

/** Deterministic event id: stable under replay, so the shared sink dedupes it.
 *
 * The canonical subject keys are part of the shared protocol — they must match
 * the Python reference sink exactly (see schemas/ledger-identity.fixture.json).
 * Matching means sorting keys at every depth, not just the top: Python's
 * ``json.dumps(sort_keys=True)`` sorts nested objects too, so a subject with a
 * nested detail would otherwise hash differently on each side.
 *
 * Only integral values belong in a subject. A float serializes differently in
 * the two languages, so a subject carrying one is not a cross-language
 * identity; the callers keep the subject to strings, integers and booleans.
 */
export function deriveEventId(parts: Record<string, unknown>): string {
  const subject = JSON.stringify(canonicalize(parts));
  return 'cmdc:' + crypto.createHash('sha256').update(subject).digest('hex').slice(0, 32);
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      const item = (value as Record<string, unknown>)[key];
      if (item === undefined) continue;
      out[key] = canonicalize(item);
    }
    return out;
  }
  return value;
}

/**
 * The event id for one native event.
 *
 * Only identity-bearing events may claim a strict id. ``subagent_progress``
 * and friends carry no native sequence here, so they are explicitly marked as
 * repeatable observations and never pretend to strict dedup: the shared core
 * must not treat them as proof.
 *
 * The subject is hashed from the *undecorated* detail. Anything added after
 * (model, proof flag) is host-local decoration, and including it would make the
 * same logical event hash differently on each side of the protocol.
 */
export function eventIdentity(
  event: string, identity: ClaimedIdentity, payload: Record<string, unknown>,
  sessionId: string, occurredAt: string, detail: Record<string, unknown>,
): { eventId: string; nonProof: boolean } {
  const native = payload?.eventId ?? payload?.event_id;
  if (typeof native === 'string' && native.trim()) {
    return { eventId: native.trim(), nonProof: false };
  }
  if (event === 'subagent_start' || event === 'subagent_stop') {
    return { eventId: `${identity.invocation_id}:${event}`, nonProof: false };
  }
  // No native id: this is an observational event, ordered by its content
  // digest and explicitly not eligible as proof.
  return {
    eventId: deriveEventId({
      host: HOST, event, session_id: sessionId,
      correlation_id: identity.tool_call_id, agent: payload?.subagentType ?? '',
      occurred_at: occurredAt, detail,
    }),
    nonProof: true,
  };
}

export function workspaces(): { workspace: string; runtime: string } {
  const workspace = process.cwd();
  return { workspace, runtime: path.join(workspace, '.tianji', 'runtime') };
}

export function ledgerPath(workspace: string): string {
  return path.join(workspace, '.tianji', 'state.jsonl');
}

export function diagnosticsPath(workspace: string): string {
  return path.join(workspace, '.tianji', 'diagnostics.jsonl');
}

/**
 * Append one canonical event under the shared ledger lock.
 *
 * Returns false when the lock could not be taken or the record was rejected —
 * never a half-written line, and never an unprotected one.
 */
export function appendRecord(workspace: string, record: LedgerRecord): boolean {
  try {
    const target = ledgerPath(workspace);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    const written = withLedgerLock(workspace, () => {
      if (ledgerHasEventId(target, record.event_id)) return false;
      fs.appendFileSync(target, JSON.stringify(record) + '\n', 'utf-8');
      return true;
    });
    return written === true;
  } catch {
    return false;
  }
}

/**
 * Exact event-id lookup over the same bounded tail the Python sink scans.
 *
 * A substring search would call two different events duplicates whenever one
 * id textually contains the other, so the ids are parsed, not matched.
 */
function ledgerHasEventId(target: string, eventId: string): boolean {
  let lines: string[];
  try {
    lines = fs.readFileSync(target, 'utf-8').split('\n');
  } catch {
    return false;
  }
  for (const line of lines.slice(-LEDGER_SCAN_LIMIT)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      if (JSON.parse(trimmed)?.event_id === eventId) return true;
    } catch {
      continue;
    }
  }
  return false;
}

/**
 * Record something that could not be identified.
 *
 * An event with no verified identity is not a ledger event: writing it to
 * state.jsonl would forge a v2 record. It goes to its own file instead, where
 * it is visible to a human and invisible to the reducer.
 */
export function appendDiagnostic(
  workspace: string, event: string, reason: string, payload: Record<string, unknown>,
): void {
  try {
    const target = diagnosticsPath(workspace);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    withLedgerLock(workspace, () => {
      fs.appendFileSync(target, JSON.stringify({
        ts: new Date().toISOString(),
        host: HOST,
        event,
        reason,
        agent: String(payload?.subagentType ?? 'unknown'),
        toolCallId: String(payload?.toolCallId ?? ''),
        detail: sanitizeDetail(payload),
      }) + '\n', 'utf-8');
      return true;
    });
  } catch {
    /* fail open */
  }
}

export function resolveModelInfo(
  subagentType: string, home = cmdcHome(),
): { model: string; source: string } {
  try {
    const file = path.join(home, 'agents', `${subagentType}.md`);
    const text = fs.readFileSync(file, 'utf-8');
    const match = text.match(/^model:\s*(.+)$/m);
    if (match) return { model: match[1].trim(), source: 'declared' };
    if (/# tianji-role-binding = "primary"/.test(text)) {
      return { model: '主模型(inherit)', source: 'inherit' };
    }
    return { model: '(unconfigured)', source: 'none' };
  } catch {
    return { model: '(agent md 不可读)', source: 'none' };
  }
}

interface ModContext {
  on(event: string, handler: (payload: unknown) => void): void;
  ui?: {
    setStatus?(text: string): void;
    notify?(text: string): void;
    capabilities?: { status?: boolean };
  };
}

export function register(
  cmd: ModContext,
  options: { meter?: (workspace: string, sessionId: string) => string } = {},
): () => void {
  const readMeter = options.meter ?? meterLine;
  const { workspace } = workspaces();
  const workers = new Map<string, WorkerState>();
  // Where each dispatch started, kept beyond the seat's own entry: the receipt
  // arrives on `tool_completed`, which can land after `subagent_stop` has already
  // dropped the seat. A receipt whose duration the worker typed itself is a claim,
  // not a measurement -- one read "~17 minutes" against a measured 3m52s.
  const seatStarts = new Map<string, number>();
  // Learned once, at the claim: the identity for a call id never changes.
  const claimed = new Map<string, ClaimedIdentity>();
  let sessionId = '';
  let errorCount = 0;

  const record = (
    event: string, agent: string, payload: Record<string, unknown>,
  ): void => {
    const toolCallId = String(payload?.toolCallId ?? '');
    let identity = toolCallId ? claimed.get(toolCallId) ?? null : null;

    if (event === 'subagent_start' && !identity) {
      identity = claimFor(workspace, sessionId, toolCallId, agent, payload);
      if (identity) claimed.set(toolCallId, identity);
    }
    if (!identity) {
      appendDiagnostic(
        workspace, event,
        event === 'subagent_start' ? 'no verifiable claim marker' : 'no claimed invocation for this call id',
        payload,
      );
      return;
    }

    const occurredAt = new Date().toISOString();
    const detail = sanitizeDetail(payload);

    // The id is derived before any host-local decoration is added, so the
    // subject stays exactly what the shared protocol hashes.
    const { eventId, nonProof } = eventIdentity(
      event, identity, payload, sessionId, occurredAt, detail,
    );
    if (nonProof) detail.proof_eligible = false;
    // The binding comes from the claim, which recorded what the dispatcher saw.
    // Only when the claim carries none (an invocation opened without a binding)
    // does this fall back to reading the role file, and that reading is a
    // display fact, never the authoritative one.
    const fallback = resolveModelInfo(agent);
    detail.model = identity.model || fallback.model;
    detail.model_source = identity.model ? (identity.model_source || 'declared') : fallback.source;

    appendRecord(workspace, {
      schema_version: SCHEMA_VERSION,
      event_id: eventId,
      event,
      host: HOST,
      run_id: identity.run_id,
      session_id: sessionId,
      task_id: identity.task_id,
      attempt: identity.attempt,
      invocation_id: identity.invocation_id,
      correlation_id: identity.tool_call_id,
      agent,
      occurred_at: occurredAt,
      recorded_at: occurredAt,
      detail,
    });
  };

  const claimFor = (
    target: string, session: string, toolCallId: string, agent: string,
    payload: Record<string, unknown>,
  ): ClaimedIdentity | null => {
    if (!toolCallId) return null;
    const token = extractClaimToken(payload?.description);
    if (!token) return null;
    const outcome = claimInvocation({
      workspace: target, host: HOST, sessionId: session,
      token, toolCallId, role: agent && agent !== 'unknown' ? agent : undefined,
    });
    if (!outcome.ok || !outcome.identity) {
      errorCount++;
      return null;
    }
    return outcome.identity;
  };

  const refreshFooter = (): void => {
    try {
      if (!cmd?.ui?.capabilities?.status || !cmd.ui.setStatus) return;
      const now = Date.now();
      const all = [...workers.values()];
      if (all.length === 0) {
        // Nothing running means nothing can be over its ceiling, so there is no
        // reading to watch: the meter is a live instrument, and at rest it is a
        // number to be read past (it also costs a call to the shared script).
        cmd.ui.setStatus(`tianji: 待命${errorCount ? ` (mod errs:${errorCount})` : ''}`);
        return;
      }
      // The session's spend leads the line, ahead of anything that could trim
      // it: on this host the mod owns the whole status text, so this is the
      // front of it. The reading comes from the shared script, never from a copy
      // of the rule here.
      const reading = readMeter(workspace, sessionId);
      // What is running, on which model, doing what, for how long. Raw facts
      // only: whether a dispatch is healthy, slow or stuck is a judgement over
      // the shared ledger, not this footer's. Every seat that is running goes on
      // the line at once and the line is cut to the footer's own width here --
      // seat by seat, with an honest `+N` for whoever does not fit -- because a
      // line handed to the host too long comes back with its last seats missing.
      cmd.ui.setStatus(composeSeats(all, reading, FOOTER_WIDTH, now));
    } catch {
      /* fail open */
    }
  };

  // A quiet worker produces no events, so the footer is refreshed on a timer.
  // It observes and reports; it never decides anything.
  const timer = setInterval(() => {
    try {
      refreshFooter();
    } catch {
      /* fail open */
    }
  }, 5000);
  if (typeof (timer as { unref?: () => void }).unref === 'function') {
    (timer as { unref: () => void }).unref();
  }

  const handlers: Array<[string, (payload: Record<string, unknown>) => void]> = [
    ['run_start', payload => {
      sessionId = String(payload?.sessionId ?? '');
      refreshFooter();
    }],
    ['subagent_start', payload => {
      const role = String(payload?.subagentType ?? 'unknown');
      const id = String(payload?.toolCallId ?? '');
      const info = resolveModelInfo(role);
      record('subagent_start', role, payload);
      const identity = id ? claimed.get(id) : undefined;
      if (identity) {
        workers.set(id, {
          steps: 0,
          role,
          // The claim recorded the binding the dispatcher saw; the role file is
          // only a display fallback when the invocation carried none.
          model: identity.model || info.model,
          modelSource: identity.model ? (identity.model_source || 'declared') : info.source,
          taskId: identity.task_id,
          toolCallId: id,
          startTs: new Date().toISOString(), lastAction: '',
          lastActivityMs: Date.now(),
        });
      }
      // Recorded outside the identity check: an unattributed seat still started,
      // and the receipt's duration must not depend on the claim being found.
      if (id) seatStarts.set(id, Date.now());
      refreshFooter();
    }],
    ['subagent_progress', payload => {
      const id = String(payload?.toolCallId ?? '');
      const worker = id ? workers.get(id) : undefined;
      if (worker) {
        worker.lastAction = actionLabel(
          payload?.toolName ?? worker.lastAction, payload?.toolInput,
        );
        // One progress event per child tool call, and it names the call it
        // belongs to -- so this count survives parallel seats, which a turn
        // count would not.
        worker.steps++;
        worker.lastActivityMs = Date.now();
      }
      // The count is added to the row instead of forwarded from it: no host
      // payload carries it, and the call meters read the ledger, not the footer.
      record('subagent_progress', String(payload?.subagentType ?? 'unknown'),
        worker && payload && typeof payload === 'object'
          ? { ...(payload as Record<string, unknown>), toolCalls: worker.steps }
          : payload);
      refreshFooter();
    }],
    ['subagent_stop', payload => {
      record('subagent_stop', String(payload?.subagentType ?? 'unknown'), payload);
      const id = String(payload?.toolCallId ?? '');
      if (!id) return;
      workers.delete(id);
      refreshFooter();
    }],
    ['tool_completed', payload => {
      if (payload?.toolName === 'agent') {
        const id = String(payload?.toolCallId ?? '');
        const startedMs = seatStarts.get(id);
        seatStarts.delete(id);
        record('agent_receipt', 'agent', {
          toolCallId: id,
          receipt: truncateReceipt(payload?.result),
          // Measured on the host clock, never typed by the worker: the start/stop
          // pair in the ledger is the only duration that counts, and a worker's
          // own estimate is not evidence.
          durationMs: startedMs === undefined ? null : Math.max(0, Date.now() - startedMs),
          durationSource: startedMs === undefined ? 'unavailable' : 'measured_host_clock',
        });
      }
    }],
  ];

  for (const [name, handler] of handlers) {
    try {
      cmd.on(name, (payload: unknown) => {
        try {
          handler((payload ?? {}) as Record<string, unknown>);
        } catch (error) {
          errorCount++;
          appendDiagnostic(workspace, 'mod_error', String(error).slice(0, 300), {});
        }
      });
    } catch {
      /* fail open */
    }
  }

  return () => {
    clearInterval(timer);
    workers.clear();
    claimed.clear();
  };
}

export default function (cmd: ModContext): void {
  register(cmd);
}
