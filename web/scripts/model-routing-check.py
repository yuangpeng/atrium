#!/usr/bin/env python3
"""Exercise model-driven runtime selection against an isolated Atrium instance.

Start a scratch instance with a fixture conversation as described in AGENTS.md.
Then run with Playwright installed:
  python web/scripts/model-routing-check.py --base http://127.0.0.1:8841 --shots /tmp/model-routing
All session mutations are intercepted; this test never starts a real model turn.
"""
import argparse
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--base', default='http://127.0.0.1:8841')
parser.add_argument('--shots', type=Path, default=Path('/tmp/atrium-model-routing'))
parser.add_argument('--webkit')
args = parser.parse_args()
args.shots.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location('duo', Path(__file__).with_name('duo-check.py'))
duo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(duo)
base = args.base.rstrip('/')
pid = duo.get(base + '/api/projects')[0]['id']
source = next(r for r in duo.get(base + '/api/p/' + pid + '/sessions') if r['role'] == 'worker')
sid = source['id']
raw_snapshot = duo.get(base + '/api/p/' + pid + '/sessions/' + sid)
raw_meta = duo.get(base + '/api/meta')
models = {'codex': 'gpt-fixture-with-a-long-model-name', 'claude-code': 'claude-fixture-with-a-long-model-name', 'antigravity': 'gemini-fixture-with-a-long-model-name'}
labels = {'codex': 'Codex', 'claude-code': 'Claude Code', 'antigravity': 'Antigravity'}


async def setup(ctx):
    state = {'live': 'idle', 'gone': False, 'model': 'legacy-codex-model', 'created': [], 'sent': [],
             'model_calls': [], 'new': [], 'fail_create': False, 'fail_send': False,
             'fail_model': False, 'send_gate': None}

    def row():
        return {**source, 'harness': 'codex', 'live': state['live'], 'workspace_gone': state['gone'], 'pending': 0}

    async def route_api(route):
        req = route.request
        path = urlparse(req.url).path
        if path == '/api/meta':
            meta = copy.deepcopy(raw_meta)
            meta['harnesses'] = []
            meta['settings']['defaults']['harness'] = 'codex'
            meta['settings']['harnesses'] = {}
            for hid, model in models.items():
                levels = ['low', 'medium', 'high'] if hid != 'codex' else ['low', 'xhigh']
                meta['harnesses'].append({'id': hid, 'label': labels[hid], 'available': True,
                    'permission_modes': [{'id': hid + '-mode', 'label': 'Full access'}],
                    'models': [{'id': '', 'label': 'Default'}, {'id': model, 'label': model, 'efforts': levels}],
                    'efforts': levels, 'config_model': model, 'can_fork': True, 'can_edit': True})
                meta['settings']['harnesses'][hid] = {'model': model if hid == 'codex' else 'different-default-model',
                    'permission_mode': hid + '-mode', 'effort': levels[-1]}
            for hid in ('pi', 'dsh'):
                duplicate = copy.deepcopy(meta['harnesses'][0])
                duplicate.update(id=hid, label=hid.upper(), models=[{'id': 'relay/' + models['codex'], 'label': models['codex']}])
                meta['harnesses'].append(duplicate)
            await route.fulfill(json=meta)
        elif path.endswith('/sessions') and req.method == 'GET':
            await route.fulfill(json=[row()] + state['new'])
        elif path.endswith('/sessions/' + sid) and req.method == 'GET':
            snap = copy.deepcopy(raw_snapshot)
            snap.update(session=row(), live=True, state=state['live'], pending=[],
                settings={'model': state['model'], 'mode': 'codex-mode', 'effort': 'xhigh'})
            snap['items'] = [
                {'kind': 'user', 'id': 'original', 'text': 'Original task: implement a feature. Full requirements.'},
                {'kind': 'thinking', 'id': 'thinking', 'text': 'HIDDEN_REASONING_SENTINEL'},
                {'kind': 'system', 'id': 'compact', 'subtype': 'compact_summary', 'text': 'Earlier summary', 'detail': 'Remaining: add tests.'},
                {'kind': 'tool', 'id': 'tool', 'name': 'exec_command', 'input': {'cmd': 'git status --short'}},
                {'kind': 'tool_result', 'id': 'result', 'content': ' M web/src/feature.tsx', 'is_error': False},
                {'kind': 'text', 'id': 'reply', 'text': 'Ready for the next step.'},
            ]
            await route.fulfill(json=snap)
        elif path.endswith('/model') and req.method == 'POST':
            state['model_calls'].append(req.post_data_json)
            if state['fail_model']:
                state['fail_model'] = False
                await route.fulfill(status=400, json={'detail': 'Fixture model rejected'})
            else:
                state['model'] = req.post_data_json['model']
                await route.fulfill(json={'ok': True})
        elif path.endswith('/effort') and req.method == 'POST':
            await route.fulfill(json={'ok': True})
        elif path.endswith('/sessions') and req.method == 'POST':
            body = req.post_data_json
            state['created'].append(body)
            if state['fail_create']:
                state['fail_create'] = False
                await route.fulfill(status=400, json={'detail': 'Fixture startup rejected'})
            else:
                new_id = 'model-handoff-' + str(len(state['created']))
                state['new'].append({**row(), 'id': new_id, 'harness': body['harness'], 'name': body.get('name', ''), 'test_model': body['model']})
                await route.fulfill(json={'id': new_id, 'cwd': source['cwd']})
        elif path.endswith('/send') and req.method == 'POST':
            state['sent'].append(req.post_data_json)
            if state['send_gate']:
                await state['send_gate'].wait()
            if state['fail_send']:
                state['fail_send'] = False
                await route.fulfill(status=500, json={'detail': 'Fixture first message failed'})
            else:
                await route.fulfill(json={'ok': True})
        elif '/sessions/model-handoff-' in path and req.method == 'GET':
            selected = next(r for r in state['new'] if r['id'] == path.split('/')[-1])
            snap = copy.deepcopy(raw_snapshot)
            snap.update(session=selected, live=True, state='idle', pending=[], settings={'model': selected['test_model'], 'effort': 'high', 'mode': selected['harness'] + '-mode'})
            await route.fulfill(json=snap)
        elif req.method not in ('GET', 'HEAD'):
            await route.fulfill(status=400, json={'detail': 'Unexpected mutation in UI check'})
        else:
            await route.continue_()
    await ctx.route('**/api/**', route_api)
    return state


async def goto(page, chat=False):
    await page.goto(base + '/?project=' + pid + ('&session=' + sid if chat else ''), wait_until='networkidle')
    await expect(page.locator('.composer-box .model-chip:not(.measure):visible')).to_be_visible()


async def menu(page):
    await page.locator('.composer-box .model-chip:not(.measure):visible').click()
    options = page.locator('.model-item')
    await expect(options).to_have_count(3)
    text = '\n'.join(await options.all_inner_texts())
    assert all(model in text for model in models.values()), text
    assert 'Default' not in text and 'legacy-codex-model' not in text, text
    return options


async def choose(page, hid):
    await menu(page)
    await page.locator('.model-item').filter(has=page.get_by_text(models[hid], exact=True)).click()


async def handoff(page, hid):
    await choose(page, hid)
    await expect(page.get_by_role('dialog')).to_be_visible()
    await expect(page.get_by_label('Context to hand over')).to_be_visible()
    await expect(page.locator('.harness-handoff .model-chip:not(.measure)')).to_have_attribute('aria-label', 'Change model: ' + models[hid])
    assert 'different-default-model' not in await page.get_by_role('dialog').inner_text()


async def main():
    errors, checks, findings = [], [], []
    async with async_playwright() as pw:
        browser = await pw.webkit.launch(**({'executable_path': args.webkit} if args.webkit else {}))
        ctx = await browser.new_context(viewport={'width': 1440, 'height': 1000})
        state = await setup(ctx)
        page = await ctx.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        await goto(page)
        await page.locator('.composer-input:visible').fill('Keep the new-agent draft')
        for hid in models:
            await choose(page, hid)
            await expect(page.locator('span[aria-label="Harness: ' + labels[hid] + '"]:visible')).to_be_visible()
            await expect(page.locator('.composer-input:visible')).to_have_value('Keep the new-agent draft')
        await page.locator('.send-btn:visible').click()
        await expect(page.locator('.chat:visible')).to_be_visible()
        assert state['created'][-1]['harness'] == 'antigravity' and state['created'][-1]['model'] == models['antigravity']
        assert state['created'][-1]['mode'] == 'antigravity-mode' and state['created'][-1]['effort'] == 'high'
        checks.append('new-agent selection maps all three models, preserves the draft, and submits target-specific settings')

        await goto(page, chat=True)
        await page.locator('.composer-input:visible').fill('Keep this unsent reply')
        previous_label = await page.locator('.composer-box .model-chip:not(.measure):visible').get_attribute('aria-label')
        state['fail_model'] = True
        await choose(page, 'codex')
        await expect(page.locator('.toast.error').filter(has_text='Fixture model rejected')).to_be_visible()
        await expect(page.locator('.composer-box .model-chip:not(.measure):visible')).to_have_attribute('aria-label', previous_label)
        await page.keyboard.press('Escape')
        checks.append('a rejected model change preserves the previous selection and allows retry')
        await choose(page, 'codex')
        assert state['model_calls'][-1]['model'] == models['codex']
        await expect(page.get_by_role('dialog')).to_have_count(0)
        await expect(page.locator('.composer-input:visible')).to_have_value('Keep this unsent reply')
        checks.append('same-runtime selection updates the existing session without creating a handoff')

        state['send_gate'] = asyncio.Event()
        state['fail_send'] = True
        await page.locator('.send-btn:visible').click()
        options = await menu(page)
        for option in await options.all():
            await expect(option).to_be_disabled()
        state['send_gate'].set()
        await expect(page.locator('.composer-input:visible')).to_have_value('Keep this unsent reply')
        state['send_gate'] = None
        await page.keyboard.press('Escape')
        checks.append('an outgoing message blocks model changes; a failed send restores its draft')
        await handoff(page, 'claude-code')
        context = await page.get_by_label('Context to hand over').input_value()
        assert 'Full requirements' in context and 'Remaining: add tests.' in context and 'git status --short' in context
        assert 'HIDDEN_REASONING' not in context
        await page.get_by_role('button', name='Cancel', exact=True).click()
        await expect(page.locator('.composer-input:visible')).to_have_value('Keep this unsent reply')
        checks.append('cross-runtime selection preselects the clicked model, transfers visible context, and cancellation preserves the draft')

        await handoff(page, 'antigravity')
        state['fail_create'] = True
        await page.get_by_role('button', name='Continue', exact=True).click()
        await expect(page.locator('.toast.error').filter(has_text='Fixture startup rejected')).to_be_visible()
        await expect(page.get_by_label('What to do next')).to_have_value('Keep this unsent reply')
        state['fail_send'] = True
        await page.get_by_role('button', name='Continue', exact=True).click()
        await expect(page.get_by_role('dialog')).to_have_count(0)
        await expect(page.locator('.toast.error').filter(has_text='The message is saved in its draft')).to_be_visible()
        created = state['created'][-1]
        assert created['harness'] == 'antigravity' and created['model'] == models['antigravity'] and created['cwd'] == source['cwd']
        assert 'prompt' not in created
        assert 'Previous conversation context' in await page.locator('.composer-input:visible').input_value()
        checks.append('handoff failures preserve context; successful creation uses the exact clicked model and source directory')

        for live in ('starting', 'running', 'waiting'):
            state['live'] = live
            await goto(page, chat=True)
            await page.reload(wait_until='networkidle')
            options = await menu(page)
            for option in await options.all():
                await expect(option).to_be_disabled()
        state['live'] = 'idle'
        await goto(page, chat=True)
        await page.reload(wait_until='networkidle')
        await handoff(page, 'claude-code')
        state['live'] = 'running'
        before = len(state['created'])
        await page.get_by_role('button', name='Continue', exact=True).click()
        await expect(page.locator('.toast.error').filter(has_text='The original agent is working')).to_be_visible()
        assert len(state['created']) == before
        checks.append('working sessions block model changes, including a source resumed during handoff review')
        await ctx.close()

        for pose, (width, height, _, *extra) in {**duo.POSES, **duo.APP_POSES}.items():
            insets = extra[0] if extra else None
            ctx = await browser.new_context(viewport={'width': width, 'height': height}, is_mobile=True, has_touch=True, device_scale_factor=2)
            state = await setup(ctx)
            if insets:
                await ctx.route('**/*.css', duo.with_insets(insets))
            page = await ctx.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            await goto(page)
            for hid in models:
                await choose(page, hid)
                assert not await page.evaluate('document.documentElement.scrollWidth > innerWidth'), (pose, hid)
                await expect(page.locator('span[aria-label="Harness: ' + labels[hid] + '"]:visible')).to_be_visible()
            await menu(page)
            findings.extend({'pose': pose, 'state': 'new-model-menu', **f} for f in await page.evaluate(duo.AUDIT, [duo.MIN_TAP, insets]))
            await page.screenshot(path=str(args.shots / (pose + '-models.png')), animations='disabled')
            await goto(page, chat=True)
            await handoff(page, 'claude-code')
            findings.extend({'pose': pose, 'state': 'handoff', **f} for f in await page.evaluate(duo.AUDIT, [duo.MIN_TAP, insets]))
            await page.screenshot(path=str(args.shots / (pose + '-handoff.png')), animations='disabled')
            await page.get_by_role('button', name='Continue', exact=True).click()
            await expect(page.get_by_role('dialog')).to_have_count(0)
            assert state['created'][-1]['harness'] == 'claude-code' and state['created'][-1]['model'] == models['claude-code']
            await ctx.close()
            checks.append(pose + ': three models, automatic runtime, long-name layout, handoff and touch submission')
            print(checks[-1], flush=True)
        await browser.close()
    result = {'checks': checks, 'findings': findings, 'errors': errors}
    (args.shots / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    assert not findings and not errors


asyncio.run(main())
