// Pure-logic tests for the Command Code state mod.
// Run: node --test skills/tianji/host_adapters/cmdc/mod/tianji-state.test.mts
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';

import {
  ACTION_CHARS,
  RUNTIME_LOCATOR,
  actionLabel,
  appendDiagnostic,
  appendRecord,
  claimInvocation,
  clip,
  deriveEventId,
  diagnosticsPath,
  eventIdentity,
  extractClaimToken,
  ledgerLockPath,
  maskSecrets,
  meterLine,
  register,
  sanitizeDetail,
  shortDuration,
  shortModel,
  stripClaimToken,
  truncateReceipt,
  withLedgerLock,
} from './tianji-state.ts';

const RUN = '77802c0c-d50f-4fe8-bc09-8ea018cf2334';
const TOKEN = 'AAAAAAAAAAAAAAAAAAAAAA';

test('event ids are stable under replay and dedupe-compatible', () => {
  const parts = { host: 'cmdc', event: 'subagent_start', session_id: 's1', correlation_id: 'call-a' };
  assert.equal(deriveEventId(parts), deriveEventId({ ...parts }));
  assert.notEqual(deriveEventId(parts), deriveEventId({ ...parts, correlation_id: 'call-b' }));
  assert.match(deriveEventId(parts), /^cmdc:[0-9a-f]{32}$/);
});

test('cross-language identity fixture: same event, same id as the Python sink', () => {
  // The fixture's detail is non-empty and nested, so this fails if either side
  // only sorts its top-level keys.
  const fixturePath = new URL('../../../schemas/ledger-identity.fixture.json', import.meta.url);
  const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf-8'));
  const input = fixture.input;

  assert.equal(
    deriveEventId({
      host: input.host,
      event: input.event,
      session_id: input.session_id,
      correlation_id: input.toolCallId,
      agent: input.agent,
      occurred_at: input.ts,
      detail: input.detail,
    }),
    fixture.expected.event_id,
  );
});

test('start and stop claim a strict identity; progress does not', () => {
  const identity = {
    run_id: RUN, task_id: 'task-a', attempt: 1,
    invocation_id: 'inv-1', tool_call_id: 'call-a',
  };
  const start = eventIdentity(
    'subagent_start', identity, {}, 's1', '2026-09-10T12:00:00.000Z', {},
  );
  assert.equal(start.eventId, 'inv-1:subagent_start');
  assert.equal(start.nonProof, false);

  const stop = eventIdentity(
    'subagent_stop', identity, {}, 's1', '2026-09-10T12:00:01.000Z', {},
  );
  assert.equal(stop.eventId, 'inv-1:subagent_stop');
  assert.equal(stop.nonProof, false);

  // Progress carries no native sequence, so it must not pretend to strict
  // dedup: the shared core may not treat it as proof.
  const progress = eventIdentity(
    'subagent_progress', identity, {}, 's1', '2026-09-10T12:00:02.000Z',
    { toolName: 'read_file', tokensUsed: 3 },
  );
  assert.equal(progress.nonProof, true);
  assert.match(progress.eventId, /^cmdc:[0-9a-f]{32}$/);
});

test('a native event id is always preferred', () => {
  const identity = {
    run_id: RUN, task_id: 'task-a', attempt: 1,
    invocation_id: 'inv-1', tool_call_id: 'call-a',
  };
  const native = eventIdentity(
    'subagent_progress', identity, { eventId: 'native-42' }, 's1',
    '2026-09-10T12:00:00.000Z', {},
  );
  assert.equal(native.eventId, 'native-42');
  assert.equal(native.nonProof, false);
});

test('the claim marker is extracted only from the end and stripped from detail', () => {
  assert.equal(extractClaimToken(`做点事 [TJ:${TOKEN}]`), TOKEN);
  assert.equal(extractClaimToken(`[TJ:${TOKEN}] 做点事`), null);
  assert.equal(extractClaimToken('没有标记'), null);
  assert.equal(stripClaimToken(`做点事 [TJ:${TOKEN}]`), '做点事');

  const detail = sanitizeDetail({ description: `做点事 [TJ:${TOKEN}]`, toolName: 'x' });
  assert.equal(detail.description, '做点事');
  assert.doesNotMatch(JSON.stringify(detail), /TJ:/);
});

test('secrets are masked and detail is whitelisted and truncated', () => {
  assert.equal(maskSecrets('key sk-abcdefghijklmnop end'), 'key [MASKED] end');
  assert.equal(maskSecrets('ghp_abcdefghijklmnopqrst'), '[MASKED]');

  const detail = sanitizeDetail({
    toolName: 'shell_command',
    toolInput: 'export TOKEN=supersecretvalue123',
    ignored: 'drop me',
    tokensUsed: 42,
  });
  assert.deepEqual(Object.keys(detail).sort(), ['tokensUsed', 'toolInput', 'toolName']);
  assert.equal(detail.ignored, undefined);
  assert.doesNotMatch(String(detail.toolInput), /supersecretvalue123/);
  assert.equal(detail.tokensUsed, 42);

  const long = sanitizeDetail({ toolInput: 'x'.repeat(4000) });
  assert.equal(String(long.toolInput).length, 500);
  assert.equal(truncateReceipt('y'.repeat(9000)).length, 8000);
});

test('a worker receipt is captured and not cut down to a snippet', () => {
  const receipt = 'z'.repeat(3000);
  const detail = sanitizeDetail({ toolCallId: 'call-a', receipt });
  assert.deepEqual(Object.keys(detail), ['receipt']);
  assert.equal(String(detail.receipt).length, 3000);

  const huge = sanitizeDetail({ receipt: 'q'.repeat(20000) });
  assert.equal(String(huge.receipt).length, 8000);

  const masked = sanitizeDetail({ receipt: 'token sk-abcdefghijklmnop done' });
  assert.doesNotMatch(String(masked.receipt), /sk-abcdefghijklmnop/);
});

test('the ledger lock excludes other holders and leaves nothing behind', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tianji-lock-'));
  try {
    let concurrent = 0;
    let max = 0;
    const results = withLedgerLock(root, () => {
      concurrent++;
      max = Math.max(max, concurrent);
      concurrent--;
      return 'first';
    });
    assert.equal(results, 'first');
    assert.equal(max, 1);
    assert.equal(fs.existsSync(ledgerLockPath(root)), false);

    // A live holder is respected, not stolen from.
    fs.writeFileSync(ledgerLockPath(root), JSON.stringify({
      pid: process.pid, host: os.hostname(),
      acquired_at: Date.now() / 1000,
      lease_expires_at: Date.now() / 1000 + 3600,
    }));
    const deadline = Date.now() + 300;
    const blocked = withLedgerLock(root, () => 'never');
    assert.equal(blocked, null);
    assert.ok(Date.now() >= deadline - 50);
    fs.rmSync(ledgerLockPath(root), { force: true });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a canonical record is appended once and diagnostics stay out of the ledger', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tianji-append-'));
  try {
    const record = {
      schema_version: 2, event_id: 'inv-1:subagent_start', event: 'subagent_start',
      host: 'cmdc', run_id: RUN, session_id: 's1', task_id: 'task-a', attempt: 1,
      invocation_id: 'inv-1', correlation_id: 'call-a', agent: 'tianji-worker',
      occurred_at: '2026-09-10T12:00:00.000Z', recorded_at: '2026-09-10T12:00:00.000Z',
      detail: {},
    };
    assert.equal(appendRecord(root, record), true);
    assert.equal(appendRecord(root, record), false);
    const lines = fs.readFileSync(path.join(root, '.tianji', 'state.jsonl'), 'utf-8')
      .split('\n').filter(Boolean);
    assert.equal(lines.length, 1);
    assert.equal(JSON.parse(lines[0]).event_id, 'inv-1:subagent_start');

    appendDiagnostic(root, 'subagent_start', 'no marker', { toolCallId: 'call-z' });
    const diagnostics = fs.readFileSync(diagnosticsPath(root), 'utf-8')
      .split('\n').filter(Boolean);
    assert.equal(diagnostics.length, 1);
    assert.equal(JSON.parse(diagnostics[0]).reason, 'no marker');
    // The diagnostic never leaked into the ledger.
    const ledger = fs.readFileSync(path.join(root, '.tianji', 'state.jsonl'), 'utf-8');
    assert.equal(ledger.split('\n').filter(Boolean).length, 1);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// A stand-in for the shared registry process, so the bridge is tested without
// depending on a Python that happens to be on PATH.
const STUB = `
const fs = require('node:fs');
const raw = fs.readFileSync(0, 'utf-8');
const mode = process.argv[2];
if (mode === 'claim') {
  const request = JSON.parse(raw);
  if (process.env.STUB_MODE === 'fail') {
    process.stdout.write(JSON.stringify({ ok: false, error: 'token already consumed' }));
    process.exit(1);
  }
  if (process.env.STUB_MODE === 'garbage') {
    process.stdout.write('not json');
    process.exit(0);
  }
  if (process.env.STUB_MODE === 'hang') {
    setTimeout(() => {}, 60000);
  } else {
    process.stdout.write(JSON.stringify({
      ok: true, reused: false,
      run_id: '77802c0c-d50f-4fe8-bc09-8ea018cf2334',
      task_id: 'task-a', attempt: 1,
      // One invocation per dispatch, like the real registry: start/stop event
      // ids are built from this, so a stub that reused one id would have every
      // seat after the first silently deduped away.
      invocation_id: 'inv-' + require('node:crypto')
        .createHash('sha1').update(String(request.tool_call_id)).digest('hex').slice(0, 12),
      tool_call_id: request.tool_call_id,
      model: 'deepseek/deepseek-v4.1-flash', model_source: 'declared',
    }));
  }
}
`;

function stubLocator() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tianji-claim-'));
  const script = path.join(root, 'stub.cjs');
  fs.writeFileSync(script, STUB);
  return {
    root,
    locator: { interpreter: process.execPath, registry: script, scriptsDir: root },
  };
}

/**
 * A mod registered against a throwaway workspace, with no real host around it.
 *
 * The footer is the only place a reader sees a dispatch, so its tests need a
 * workspace, a stub claim and a way to fire host events -- and there are enough
 * of them now that repeating the setup in each test hides what is being tested.
 */
/** The line as a reader sees it: escapes take no cells and say nothing. */
const plain = (text: string): string => text.replace(/\u001b\[[0-9;]*m/g, '');

test('no combination of seats can push the line past the footer width', () => {
  // The contract is a cell budget, and the risk is a field that grows without
  // its share being checked -- a long role, a long tool, a target of any length,
  // a mark, or six seats. Sweep them rather than argue about them, and prove at
  // the same time that styling costs no cells.
  const roles = ['tianji-worker', 'tianji-verifier', 'tianji-referee', 'tianji-reviewer-a-very-long-one'];
  const tools = ['read_file', 'shell_command', 'grep', 'a_tool_name_far_longer_than_any_real_one'];
  for (const count of [1, 2, 3, 4, 5, 6, 8]) {
    for (const role of roles) {
      for (const tool of tools) {
        const seats = Array.from({ length: count }, (_, index) => seat({
          role, toolCallId: `call-${index}`, steps: index * 37,
          startTs: new Date(SEAT_NOW - (index + 1) * 61_000).toISOString(),
          lastAction: `${tool} ${'x'.repeat(40 - index)}.py`,
          lastActivityMs: SEAT_NOW - index * 120_000,
        }));
        const line = composeSeats(seats, SEAT_METER, FOOTER_WIDTH, SEAT_NOW);
        assert.ok(
          displayWidth(line) <= FOOTER_WIDTH,
          `${count} 席 ${role} ${tool}: ${displayWidth(line)} 列 > ${FOOTER_WIDTH}`,
        );
        assert.equal(displayWidth(line), displayWidth(plain(line)), 'colour spends no cells');
      }
    }
  }
});

function harness(options: { meter?: string } = {}) {
  const { root, locator } = stubLocator();
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'tianji-footer-'));
  const previousHome = process.env.CMDC_HOME;
  const previousCwd = process.cwd();
  process.env.CMDC_HOME = root;
  fs.writeFileSync(path.join(root, RUNTIME_LOCATOR), JSON.stringify(locator));
  process.chdir(workspace);

  const statuses: string[] = [];
  const handlers = new Map<string, (payload: Record<string, unknown>) => void>();
  const stop = register({
    on: (name: string, handler: (payload: Record<string, unknown>) => void) => {
      handlers.set(name, handler);
    },
    ui: {
      setStatus: (text: string) => { statuses.push(text); },
      capabilities: { status: true },
    },
  } as unknown as Parameters<typeof register>[0], { meter: () => options.meter ?? '' });

  return {
    fire: (name: string, payload: Record<string, unknown> = {}): void => {
      const handler = handlers.get(name);
      if (!handler) throw new Error(`the mod did not subscribe to ${name}`);
      handler(payload);
    },
    last: (): string => statuses[statuses.length - 1] ?? '',
    dispose: (): void => {
      stop();
      process.chdir(previousCwd);
      if (previousHome === undefined) delete process.env.CMDC_HOME;
      else process.env.CMDC_HOME = previousHome;
      fs.rmSync(root, { recursive: true, force: true });
      fs.rmSync(workspace, { recursive: true, force: true });
    },
  };
}

const METER_STUB = `
if ((process.env.STUB_MODE || 'ok') === 'fail') process.exit(3);
process.stdout.write('tok 5K/10K\\n');
`;

test('the meter is asked of the shared script, and silence is how it fails', () => {
  // The number must come from the one file that defines it (statusline.py), or
  // the two hosts drift; a reading that cannot be taken returns an empty string,
  // never a made-up figure.
  const scripts = fs.mkdtempSync(path.join(os.tmpdir(), 'tianji-meterscript-'));
  const previous = process.env.STUB_MODE;
  try {
    fs.writeFileSync(path.join(scripts, 'statusline.py'), METER_STUB);
    const locator = {
      interpreter: process.execPath,
      registry: path.join(scripts, 'run_registry.py'),
      scriptsDir: scripts,
    };

    assert.equal(meterLine(scripts, 's1', { locator }), 'tok 5K/10K');
    assert.equal(meterLine(scripts, '', { locator }), '');

    // An install that never happened is what makes a locator absent, so pointing
    // the lookup at an empty home is what proves the absence -- passing a null
    // locator would fall through to the machine's real one and read that.
    const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'tianji-nohome-'));
    assert.equal(meterLine(scripts, 's1', { home: empty }), '');
    fs.rmSync(empty, { recursive: true, force: true });

    process.env.STUB_MODE = 'fail';
    assert.equal(meterLine(scripts, 's1', { locator }), '');
  } finally {
    if (previous === undefined) delete process.env.STUB_MODE;
    else process.env.STUB_MODE = previous;
    fs.rmSync(scripts, { recursive: true, force: true });
  }
});

test('the claim bridge never uses a shell and reads the fixed JSON protocol', () => {
  const { root, locator } = stubLocator();
  try {
    const outcome = claimInvocation({
      workspace: root, host: 'cmdc', sessionId: 's1',
      token: TOKEN, toolCallId: 'call-a; rm -rf /', role: 'tianji-worker',
    }, { locator });
    assert.equal(outcome.ok, true);
    // The call id travelled as data, not as command text.
    assert.equal(outcome.identity!.tool_call_id, 'call-a; rm -rf /');
    assert.match(outcome.identity!.invocation_id, /^inv-[0-9a-f]{12}$/);
    // Distinct dispatches must get distinct invocations: the ledger's start and
    // stop ids are `${invocation_id}:${event}`, so a shared id would make the
    // second seat's rows look like duplicates of the first and drop them.
    const other = claimInvocation({
      workspace: root, host: 'cmdc', sessionId: 's1',
      token: TOKEN, toolCallId: 'call-b', role: 'tianji-worker',
    }, { locator });
    assert.notEqual(other.identity!.invocation_id, outcome.identity!.invocation_id);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a failing claim is reported, never assumed', () => {
  const { root, locator } = stubLocator();
  const previous = process.env.STUB_MODE;
  try {
    process.env.STUB_MODE = 'fail';
    const outcome = claimInvocation({
      workspace: root, host: 'cmdc', sessionId: 's1', token: TOKEN, toolCallId: 'call-a',
    }, { locator });
    assert.equal(outcome.ok, false);
    assert.match(String(outcome.error), /consumed/);

    process.env.STUB_MODE = 'garbage';
    const unreadable = claimInvocation({
      workspace: root, host: 'cmdc', sessionId: 's1', token: TOKEN, toolCallId: 'call-a',
    }, { locator });
    assert.equal(unreadable.ok, false);
  } finally {
    if (previous === undefined) delete process.env.STUB_MODE;
    else process.env.STUB_MODE = previous;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a missing runtime locator means no claim, not a guess', () => {
  // A real install writes a locator into the host home, so pointing the lookup
  // at an empty directory is what actually proves the *absence* of one. Without
  // this the test silently reached the machine's real registry: it passed only
  // until someone installed the mod for real.
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'tianji-nolocator-'));
  const previousHome = process.env.CMDC_HOME;
  try {
    process.env.CMDC_HOME = empty;
    const outcome = claimInvocation({
      workspace: process.cwd(), host: 'cmdc', sessionId: 's1',
      token: TOKEN, toolCallId: 'call-a',
    }, { locator: null });
    assert.equal(outcome.ok, false);
    assert.match(String(outcome.error), /locator/);
  } finally {
    if (previousHome === undefined) delete process.env.CMDC_HOME;
    else process.env.CMDC_HOME = previousHome;
    fs.rmSync(empty, { recursive: true, force: true });
  }
});

test('footer fields are clipped, never dropped', () => {
  assert.equal(shortModel('deepseek/deepseek-v4.1-flash'), 'deepseek-v4.1-flash');
  assert.equal(shortModel('zai-org/glm-5.3'), 'glm-5.3');
  assert.equal(shortModel(''), '?');
  assert.equal(shortDuration(0, 45_000), '45s');
  assert.equal(shortDuration(0, 80_000), '1m20s');
  const clipped = clip('飞机大战修瑕疵很长很长', 6);
  assert.equal(clipped.length, 6);
  assert.ok(clipped.endsWith('…'));
  assert.equal(clip('ab', 6), 'ab');
});

test('a seat says what its tool was pointed at, not only the tool name', () => {
  // "read_file" says a dispatch is alive and nothing more. The host's own
  // sub-agent view carries the same pair -- `recentTools: {name, input}` -- so
  // the footer can answer "what is it doing" with a fact rather than a guess.
  assert.equal(
    actionLabel('read_file', 'D:\\test\\tianji-v2\\skills\\tianji\\SKILL.md'),
    'read_file SKILL.md',
  );
  // A shell's input is a command line, not a name: it is reduced to its head
  // (the program, and the script it was pointed at) so the budget buys an answer
  // instead of a prefix of a temporary path. `read_file` is not a shell, so it
  // keeps the shape it always had.
  assert.equal(actionLabel('bash', 'python -m pytest tests -q'), 'python pytest');
  assert.equal(actionLabel('shell_command', 'git status'), 'git status');
  assert.equal(
    actionLabel('shell_command', 'cmd /c scripts\\run_registry.py --board'),
    'cmd run_registry.py',
  );
  assert.equal(
    actionLabel('shell_command', 'cd /d D:\\TEMP\\x && python -c "print(1)"'),
    'python',
  );
  assert.equal(actionLabel('grep', '  a\n  b  '), 'grep a b');
  assert.equal(actionLabel('read_file', ''), 'read_file');
  assert.equal(actionLabel('', 'whatever'), 'whatever');
  // The name of the file is the point. The first cut clipped the whole label to
  // 20, which left eleven characters for it: an ordinary file came out as
  // "read_file RUNTIME-PRO…" and told the reader nothing.
  assert.equal(
    actionLabel('read_file', 'RUNTIME-PROTOCOL.md'), 'read_file RUNTIME-PROTOCOL.md',
  );
  assert.equal(
    actionLabel('read_file', 'test_statusline_budget.py'), 'read_file test_statusline_budget.py',
  );
  // A long tool name does not eat the target's own budget.
  const long = actionLabel('list_directory', 'a'.repeat(40));
  assert.equal(long, `list_directory ${'a'.repeat(29)}…`);
  // And the whole label stays inside what a seat will carry.
  assert.ok(long.length <= ACTION_CHARS, `${long.length} > ${ACTION_CHARS}`);
});

test('the footer names the running task and its model', () => {
  // The footer is the only place a human can see what is running. It once read
  // "worker:read_file 21s 70", which names neither the task nor the model, and
  // nothing failed -- there was simply no test on this path.
  const { root, locator } = stubLocator();
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'tianji-footer-'));
  const previousHome = process.env.CMDC_HOME;
  const previousCwd = process.cwd();
  try {
    process.env.CMDC_HOME = root;
    fs.writeFileSync(path.join(root, RUNTIME_LOCATOR), JSON.stringify(locator));
    process.chdir(workspace);

    const statuses: string[] = [];
    const handlers = new Map<string, (payload: Record<string, unknown>) => void>();
    const stop = register({
      on: (name: string, handler: (payload: Record<string, unknown>) => void) => {
        handlers.set(name, handler);
      },
      ui: {
        setStatus: (text: string) => { statuses.push(text); },
        capabilities: { status: true },
      },
    } as unknown as Parameters<typeof register>[0]);

    try {
      handlers.get('run_start')!({ sessionId: 's1' });
      handlers.get('subagent_start')!({
        toolCallId: 'call-a',
        subagentType: 'tianji-worker',
        description: `干活 [TJ:${TOKEN}]`,
      });

      const running = statuses[statuses.length - 1];
      // No count of running seats: the line already lists them. Turns took its
      // place, and this dispatch has none to show (no turn events fired).
      assert.ok(!running.includes('跑'), running);
      assert.match(running, /worker@deepseek-v4\.1-flash/);
      assert.match(running, /task-a/);

      handlers.get('subagent_stop')!({ toolCallId: 'call-a', subagentType: 'tianji-worker' });
      assert.equal(statuses[statuses.length - 1], 'tianji: 待命');
    } finally {
      stop();
    }
  } finally {
    process.chdir(previousCwd);
    if (previousHome === undefined) delete process.env.CMDC_HOME;
    else process.env.CMDC_HOME = previousHome;
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(workspace, { recursive: true, force: true });
  }
});

test('the footer leads with the meter the shared script reports', () => {
  // The mod draws this footer; statusline.py draws the other host's. The meter
  // went into that script first and this path never asked for it, so on Command
  // Code the change was invisible: a fix the reader could not see.
  const { root, locator } = stubLocator();
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'tianji-meterfooter-'));
  const previousHome = process.env.CMDC_HOME;
  const previousCwd = process.cwd();
  try {
    process.env.CMDC_HOME = root;
    fs.writeFileSync(path.join(root, RUNTIME_LOCATOR), JSON.stringify(locator));
    process.chdir(workspace);

    const statuses: string[] = [];
    const handlers = new Map<string, (payload: Record<string, unknown>) => void>();
    const stop = register({
      on: (name: string, handler: (payload: Record<string, unknown>) => void) => {
        handlers.set(name, handler);
      },
      ui: {
        setStatus: (text: string) => { statuses.push(text); },
        capabilities: { status: true },
      },
    } as unknown as Parameters<typeof register>[0], { meter: () => 'tok 1M/2.1M' });

    try {
      handlers.get('run_start')!({ sessionId: 's1' });
      // At rest there is nothing running to be over its ceiling, so the line
      // carries no reading at all -- just the fact that nothing is running.
      assert.equal(statuses[statuses.length - 1], 'tianji: 待命');

      handlers.get('subagent_start')!({
        toolCallId: 'call-a',
        subagentType: 'tianji-worker',
        description: `干活 [TJ:${TOKEN}]`,
      });
      // Ahead of the running seats, which is where it has to be: a status line
      // is cut from the right, and a meter that gets trimmed away tells nobody.
      assert.match(plain(statuses[statuses.length - 1]), /^tianji: tok 1M\/2\.1M worker@/);
    } finally {
      stop();
    }
  } finally {
    process.chdir(previousCwd);
    if (previousHome === undefined) delete process.env.CMDC_HOME;
    else process.env.CMDC_HOME = previousHome;
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(workspace, { recursive: true, force: true });
  }
});

test('a meter that cannot be read is left out rather than invented', () => {
  const { root, locator } = stubLocator();
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'tianji-nometer-'));
  const previousHome = process.env.CMDC_HOME;
  const previousCwd = process.cwd();
  try {
    process.env.CMDC_HOME = root;
    fs.writeFileSync(path.join(root, RUNTIME_LOCATOR), JSON.stringify(locator));
    process.chdir(workspace);

    const statuses: string[] = [];
    const handlers = new Map<string, (payload: Record<string, unknown>) => void>();
    const stop = register({
      on: (name: string, handler: (payload: Record<string, unknown>) => void) => {
        handlers.set(name, handler);
      },
      ui: {
        setStatus: (text: string) => { statuses.push(text); },
        capabilities: { status: true },
      },
    } as unknown as Parameters<typeof register>[0], { meter: () => '' });

    try {
      handlers.get('run_start')!({ sessionId: 's1' });
      assert.equal(statuses[statuses.length - 1], 'tianji: 待命');
    } finally {
      stop();
    }
  } finally {
    process.chdir(previousCwd);
    if (previousHome === undefined) delete process.env.CMDC_HOME;
    else process.env.CMDC_HOME = previousHome;
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(workspace, { recursive: true, force: true });
  }
});

test('a seat shows how many child tool calls it has made', () => {
  // The footer led with "1跑" -- a count of the seats, which the line already
  // lists, so it told a reader nothing. Turns are what the host's own cap counts
  // and what a run is billed in, but this host does not relay them to mods
  // (its translator forwards only tool_queued, and subagent_stop carries just
  // toolCallId/subagentType/tokensUsed), so the seat shows the child tool calls
  // it can actually attribute -- labelled as steps, not as turns.
  const mod = harness();
  try {
    mod.fire('run_start', { sessionId: 's1' });
    mod.fire('subagent_start', {
      toolCallId: 'call-a', subagentType: 'tianji-verifier',
      description: `审这个变更集 [TJ:${TOKEN}]`,
    });
    for (let index = 0; index < 7; index++) {
      mod.fire('subagent_progress', {
        toolCallId: 'call-a', subagentType: 'tianji-verifier',
        toolName: 'read_file', toolInput: `file-${index}.md`, tokensUsed: index * 100,
      });
    }

    const line = mod.last();
    assert.match(line, /verifier@/);
    assert.match(line, /7步/);
    assert.ok(!line.includes('轮'), line);
    assert.ok(!line.includes('跑'), line);

    // The same count is what the ledger's call meters read back, so it goes into
    // every progress row -- cumulatively, because the readers take the max.
    const rows = fs.readFileSync(path.join(process.cwd(), '.tianji', 'state.jsonl'), 'utf-8')
      .split('\n').filter(Boolean).map(text => JSON.parse(text));
    const progress = rows.filter(row => row.event === 'subagent_progress');
    assert.equal(progress.length, 7);
    assert.deepEqual(progress.map(row => row.detail.toolCalls), [1, 2, 3, 4, 5, 6, 7]);
  } finally {
    mod.dispose();
  }
});

function ledgerRows(): Record<string, any>[] {
  return fs.readFileSync(path.join(process.cwd(), '.tianji', 'state.jsonl'), 'utf-8')
    .split('\n').filter(Boolean).map(text => JSON.parse(text));
}

test('a receipt carries the measured duration, not the worker\'s own estimate', () => {
  const mod = harness({ meter: 'tok 1M/2.1M' });
  try {
    mod.fire('run_start', { sessionId: 's1' });
    mod.fire('subagent_start', {
      toolCallId: 'call-a', subagentType: 'tianji-worker', description: `干 [TJ:${TOKEN}]`,
    });
    mod.fire('tool_completed', {
      toolName: 'agent',
      toolCallId: 'call-a',
      result: '回执：完事 / 工具调用 4 次 / 时间自估 ~17 分钟',
    });
    const receipt = ledgerRows().find(row => row.event === 'agent_receipt');
    assert.ok(receipt, 'the receipt is on the ledger');
    // The duration is the host's measurement of this dispatch. Whatever the
    // receipt text says about time stays prose: one worker wrote "~17 分钟" for a
    // dispatch the ledger clocked at 3m52s, and a claim is not evidence.
    assert.equal(receipt.detail.durationSource, 'measured_host_clock');
    assert.equal(typeof receipt.detail.durationMs, 'number');
    assert.ok(receipt.detail.durationMs >= 0, String(receipt.detail.durationMs));
    assert.ok(receipt.detail.receipt.includes('~17 分钟'), 'the claim is kept as text');
  } finally {
    mod.dispose();
  }
});

// The `unavailable` branch (a claimed call whose `subagent_start` never arrived)
// stays unexercised: the mod writes unattributable events as diagnostics, not
// ledger rows, so there is nothing here to assert against. Declared, not faked.

test('two seats at once each count their own steps', () => {
  // This is what a turn count could not do: progress events name the call they
  // belong to, so parallel seats stay separate instead of being guessed apart.
  const mod = harness();
  try {
    mod.fire('run_start', { sessionId: 's1' });
    mod.fire('subagent_start', {
      toolCallId: 'call-a', subagentType: 'tianji-verifier',
      description: `审 [TJ:${TOKEN}]`,
    });
    mod.fire('subagent_start', {
      toolCallId: 'call-b', subagentType: 'tianji-worker',
      description: `干 [TJ:${TOKEN}]`,
    });
    for (let index = 0; index < 2; index++) {
      mod.fire('subagent_progress', {
        toolCallId: 'call-a', subagentType: 'tianji-verifier',
        toolName: 'grep', toolInput: `pattern-${index}`, tokensUsed: 100,
      });
    }
    for (let index = 0; index < 5; index++) {
      mod.fire('subagent_progress', {
        toolCallId: 'call-b', subagentType: 'tianji-worker',
        toolName: 'read_file', toolInput: `file-${index}.md`, tokensUsed: 100,
      });
    }

    const line = mod.last();
    // Two seats are on the line at once, in the order they started, each with
    // what it last pointed a tool at and no spend of its own: the meter at the
    // front of the line already answers "how much". Neither the model nor the
    // task number is repeated per seat at this width -- both leave the line when
    // a second seat arrives, because the model is constant per role and the task
    // number is the least dense field there is (the board has the roster).
    // `read_file` keeps its whole name: a tool cut mid-word stops saying what
    // the seat is doing, and the target is the field that gives way instead.
    assert.match(plain(line), /^tianji: ▸ ver \d+s 2步 grep pattern-1 │ wor \d+s 5步 read_file file-4\.md$/);
    assert.ok(!line.includes('@deepseek'), line);
    assert.ok(!line.includes('task-a'), line);
  } finally {
    mod.dispose();
  }
});

// The real cross-language boundary: Python mints the claim, Node consumes it.
// Both runtimes ship with the product, so this is the contract that actually
// has to hold; it skips rather than fails when no Python is discoverable.
function findPython(): string | null {
  for (const candidate of [process.env.PYTHON, 'python', 'python3', 'py']) {
    if (!candidate) continue;
    const probe = spawnSync(candidate, ['--version'], { encoding: 'utf-8' });
    if (!probe.error && probe.status === 0) return candidate;
  }
  return null;
}

test('a real Python mint is claimed end to end, and only once', (t) => {
  const python = findPython();
  if (!python) {
    t.skip('no python interpreter available');
    return;
  }
  const scripts = path.resolve('skills/tianji/scripts');
  const registry = path.join(scripts, 'run_registry.py');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tianji-realclaim-'));
  const mint = () => JSON.parse(spawnSync(python, [
    registry, 'open', '--workspace', root, '--host', 'cmdc',
    '--session', 's1', '--role', 'tianji-worker', '--task-id', 'task-a',
  ], { encoding: 'utf-8' }).stdout);

  try {
    const opened = mint();
    assert.equal(opened.claim_token.length, 22);
    assert.equal(extractClaimToken(opened.marker), opened.claim_token);

    const locator = { interpreter: python, registry };
    const first = claimInvocation({
      workspace: root, host: 'cmdc', sessionId: 's1',
      token: opened.claim_token, toolCallId: 'call-real', role: 'tianji-worker',
    }, { locator });
    assert.equal(first.ok, true);
    assert.equal(first.identity!.invocation_id, opened.invocation_id);

    const retry = claimInvocation({
      workspace: root, host: 'cmdc', sessionId: 's1',
      token: opened.claim_token, toolCallId: 'call-real', role: 'tianji-worker',
    }, { locator });
    assert.equal(retry.ok, true);
    assert.equal(retry.reused, true);
    assert.equal(retry.identity!.invocation_id, opened.invocation_id);

    const stolen = claimInvocation({
      workspace: root, host: 'cmdc', sessionId: 's1',
      token: opened.claim_token, toolCallId: 'call-other',
    }, { locator });
    assert.equal(stolen.ok, false);
    assert.match(String(stolen.error), /already been consumed/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// The footer's whole line
//
// The footer is one line and the host cuts it from the right, so an over-long
// line does not look broken -- it silently loses its last seats, which is how
// two parallel seats turned into one and the `+N` counting the rest went with
// them. These tests pin the whole-line budget, the ladder it degrades on, and
// the one-seat line that must not change at all. The helpers are imported here
// rather than at the top of the file so this contract stays in one place.
// ---------------------------------------------------------------------------
import {
  C_STEPS, C_TIME, C_TOOL, C_VERIFIER,
  composeSeats, displayWidth, FOOTER_WIDTH, SEAT_MAX_SEATS,
} from './tianji-state.ts';
import type { WorkerState } from './tianji-state.ts';

const SEAT_NOW = Date.UTC(2026, 8, 10, 12, 0, 0);
const SEAT_METER = 'tok 10.2M/25.9M';
const LONG_TARGET = 'run_registry_with_a_very_long_name.py';

test('every field on a seat carries its own colour', () => {
  // The reader's own report: the role came out coloured and the rest of the line
  // -- action, clock, steps -- stayed plain, so "what is it doing" and "how long
  // has it been at it" read with the same weight as the separators. Each field
  // now has a colour of its own, and the one rule still holds: colour spends no
  // cells.
  const line = composeSeats(fleet(2), SEAT_METER, FOOTER_WIDTH, SEAT_NOW);
  const labels: [string, string][] = [
    ['role', C_VERIFIER], ['clock', C_TIME], ['steps', C_STEPS], ['tool', C_TOOL],
  ];
  for (const [what, code] of labels) {
    assert.ok(line.includes(code), `${what} is not coloured: ${line}`);
  }
  assert.equal(displayWidth(line), displayWidth(plain(line)), 'colour spends no cells');
});

function seat(over: Partial<WorkerState> = {}): WorkerState {
  return {
    role: 'tianji-worker',
    model: 'deepseek/deepseek-v4.1-flash',
    modelSource: 'declared',
    taskId: 'task-a',
    toolCallId: 'call-a',
    startTs: new Date(SEAT_NOW - 600_000).toISOString(),
    lastAction: `read_file ${LONG_TARGET}`,
    lastActivityMs: SEAT_NOW,
    steps: 12,
    ...over,
  };
}

/** Seats in start order: the first is the one that has been running longest. */
function fleet(count: number): WorkerState[] {
  const roles = [
    'tianji-worker', 'tianji-verifier', 'tianji-planner',
    'tianji-reviewer', 'tianji-prober', 'tianji-scout',
  ];
  return Array.from({ length: count }, (_, index) => seat({
    role: roles[index % roles.length],
    toolCallId: `call-${index}`,
    startTs: new Date(SEAT_NOW - (600_000 - index * 60_000)).toISOString(),
    steps: 12 + index,
  }));
}

/** The line the footer drew before this change: two seats, no budget, no cut. */
function legacyFooter(seats: WorkerState[], meter: string, nowMs: number): string {
  const reading = meter ? `${meter} ` : '';
  const parts = seats.slice(0, 2).map(worker => [
    `${worker.role.replace('tianji-', '')}@${shortModel(worker.model)}`,
    clip(worker.taskId || '-', 12),
    clip(worker.lastAction || '-', ACTION_CHARS),
    shortDuration(Date.parse(worker.startTs) || nowMs, nowMs),
    `${worker.steps}步`,
  ].join(' '));
  const tail = seats.length - parts.length > 0 ? ` +${seats.length - parts.length}` : '';
  return `tianji: ${reading}${parts.join(' | ')}${tail}`;
}

/** What every footer line must satisfy, whoever drew it. */
function assertFooterContract(line: string, seats: WorkerState[]): void {
  assert.ok(
    displayWidth(line) <= FOOTER_WIDTH,
    `${displayWidth(line)} 列 > ${FOOTER_WIDTH}: ${line}`,
  );
  for (const worker of seats.slice(0, SEAT_MAX_SEATS)) {
    const role = worker.role.replace('tianji-', '').slice(0, 3);
    assert.ok(line.includes(role), `${worker.role} 不在行里: ${line}`);
  }
}

test('one seat keeps the footer it has always had, byte for byte', () => {
  const only = seat({
    startTs: new Date(SEAT_NOW - 80_000).toISOString(),
    lastAction: 'read_file SKILL.md',
    steps: 7,
  });
  const lone = composeSeats([only], SEAT_METER, FOOTER_WIDTH, SEAT_NOW);
  // The host prints escapes verbatim, so the layout is what must not move: with
  // the colour stripped, this is the line the footer has shown since before the
  // compact layout existed. Shortening the plain text is a regression; colouring
  // it is not.
  assert.equal(
    plain(lone),
    'tianji: tok 10.2M/25.9M worker@deepseek-v4.1-flash task-a read_file SKILL.md 1m20s 7步',
  );
  assert.ok(lone.includes('\u001b['), 'a lone seat is styled, not plain');

  // The same line has to come out of the mod's own footer, not only out of the
  // pure function: the wiring is what the reader sees.
  const mod = harness({ meter: 'tok 1M/2.1M' });
  try {
    mod.fire('run_start', { sessionId: 's1' });
    mod.fire('subagent_start', {
      toolCallId: 'call-a', subagentType: 'tianji-verifier', description: `审 [TJ:${TOKEN}]`,
    });
    for (let index = 0; index < 7; index++) {
      mod.fire('subagent_progress', {
        toolCallId: 'call-a', subagentType: 'tianji-verifier',
        toolName: 'read_file', toolInput: `file-${index}.md`, tokensUsed: 100,
      });
    }
    assert.equal(
      plain(mod.last()),
      'tianji: tok 1M/2.1M verifier@deepseek-v4.1-flash task-a read_file file-6.md 0s 7步',
    );
  } finally {
    mod.dispose();
  }
});

test('every parallel seat is on the line, and the line fits the footer', () => {
  for (const count of [1, 2, 4, 6]) {
    const seats = fleet(count);
    const line = composeSeats(seats, SEAT_METER, FOOTER_WIDTH, SEAT_NOW);
    // The line as the footer would draw it: this is the evidence a reader can
    // compare against the line on their own screen.
    console.log(`N=${count} (${displayWidth(line)} 列): ${line}`);
    assertFooterContract(line, seats);
    if (count > SEAT_MAX_SEATS) {
      assert.match(plain(line), new RegExp(` \\+${count - SEAT_MAX_SEATS}$`), line);
    } else if (count > 1) {
      for (const worker of seats) {
        const role = worker.role.replace('tianji-', '').slice(0, 3);
        assert.ok(line.includes(role), `${count} 席缺 ${worker.role}: ${line}`);
      }
    }
  }
});

test('what a seat points at gives way in rungs before a seat does', () => {
  const seats = fleet(2);
  const targetIn = (width: number): string => {
    const first = plain(composeSeats(seats, '', width, SEAT_NOW)).split(' │ ')[0];
    const match = /read_file(?: (.*))?$/.exec(first);
    assert.ok(match, first);
    return match[1] ?? '';
  };
  // One rung per width, and the rung is chosen against the whole line rather
  // than guessed field by field: 30 -> 20 -> 12 -> 8 -> gone. The widths are
  // read off the built line -- the geometry moved when the tool stopped being
  // cut -- but the rule they check holds at any geometry: a narrower line never
  // keeps more of the target than a wider one.
  assert.equal(targetIn(130), clip(LONG_TARGET, 30));
  assert.equal(targetIn(110), clip(LONG_TARGET, 20));
  assert.equal(targetIn(95), clip(LONG_TARGET, 12));
  assert.equal(targetIn(82), clip(LONG_TARGET, 8));
  assert.equal(targetIn(70), '');
  const swept = [130, 110, 95, 82, 70].map(targetIn).map(text => text.length);
  for (let i = 1; i < swept.length; i++) {
    assert.ok(swept[i] <= swept[i - 1], `a rung grew as the line narrowed: ${swept.join(',')}`);
  }
});

test('six seats keep the four that have been running longest, and say +2', () => {
  const seats = fleet(6);
  const line = composeSeats(seats, SEAT_METER, FOOTER_WIDTH, SEAT_NOW);
  assert.match(plain(line), / \+2$/, line);
  for (const role of ['wor', 'ver', 'pla', 'rev']) assert.ok(line.includes(role), line);
  // The newest seats are the ones that go: a seat that has just started has the
  // least to report, while the seat most likely to be stuck is the oldest.
  assert.ok(!line.includes('pro'), line);
  assert.ok(!line.includes('sco'), line);
});

test('seats keep the order they started in, whatever order they arrive', () => {
  const seats = fleet(4);
  const line = composeSeats(seats, SEAT_METER, FOOTER_WIDTH, SEAT_NOW);
  assert.equal(
    composeSeats([seats[2], seats[0], seats[3], seats[1]], SEAT_METER, FOOTER_WIDTH, SEAT_NOW),
    line,
  );
  const positions = ['wor', 'ver', 'pla', 'rev'].map(role => line.indexOf(role));
  assert.deepEqual(positions, [...positions].sort((a, b) => a - b));
});

test('a seat that has gone quiet is marked, never judged', () => {
  const line = (quietMs: number): string => composeSeats([
    seat({ lastActivityMs: SEAT_NOW - quietMs, lastAction: 'read_file a.py' }),
    seat({
      role: 'tianji-verifier', toolCallId: 'call-b',
      startTs: new Date(SEAT_NOW - 60_000).toISOString(),
      lastActivityMs: SEAT_NOW, lastAction: 'grep b.py',
    }),
  ], '', FOOTER_WIDTH, SEAT_NOW);
  assert.ok(!line(30_000).includes('!'), line(30_000));
  assert.match(line(61_000), /!/);
  assert.match(line(301_000), /!!/);
  // A mark carries the raw fact -- this seat has been silent this long -- and is
  // not a verdict: whether it is stuck is the shared layer's call, not the footer's.
  assert.ok(!line(301_000).includes('卡'), line(301_000));
});

test('the old footer is what the whole-line contract catches', () => {
  // The shape that shipped: every seat as wide as its own fields, only the first
  // two of them, and no cut of its own -- the host did the cutting, which is how
  // the reader lost the seats after the second one and the `+N` with them. The
  // contract above refuses exactly this, which is what makes it a test rather
  // than a decoration (fed the old line it goes red: 217 columns, and the third
  // and fourth seats are not on it at all).
  const seats = fleet(4);
  const old = legacyFooter(seats, SEAT_METER, SEAT_NOW);
  assert.ok(displayWidth(old) > FOOTER_WIDTH, `${displayWidth(old)} 列: ${old}`);
  for (const role of ['pla', 'rev']) assert.ok(!old.includes(role), old);
  // ...and the line this change draws passes that same contract.
  assertFooterContract(composeSeats(seats, SEAT_METER, FOOTER_WIDTH, SEAT_NOW), seats);
});
