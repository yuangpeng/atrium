#!/usr/bin/env python3
"""Archive/Undo behavior and touch layout checks against an isolated Atrium instance.

Start a scratch project with a fixture conversation (AGENTS.md), then run:
  uv run --no-project --with playwright==1.64.1 python web/scripts/archive-undo-check.py --base http://127.0.0.1:8846
Edge cases use intercepted session mutations; the final round trip archives and restores the
scratch fixture through the real API. Only run this against an isolated test instance.
"""
import argparse
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright, expect

spec = importlib.util.spec_from_file_location('duo', Path(__file__).with_name('duo-check.py'))
duo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(duo)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:8846')
    parser.add_argument('--shots', type=Path, default=Path('/tmp/atrium-archive-undo'))
    parser.add_argument('--webkit')
    args = parser.parse_args()
    args.shots.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip('/')
    pid = duo.get(base + '/api/projects')[0]['id']
    source = next(s for s in duo.get(f'{base}/api/p/{pid}/sessions') if s['role'] == 'worker')
    sid = source['id']
    snapshot = duo.get(f'{base}/api/p/{pid}/sessions/{sid}')
    errors, checks = [], []

    async def setup(ctx, long_name=False):
        rows = {sid: {**source, 'name': 'Archive fixture' + ('海' * 70 if long_name else ''), 'live': None, 'archived': False},
                'fixture-b': {**source, 'id': 'fixture-b', 'name': 'Second fixture', 'live': None, 'archived': False}}
        state = {'rows': rows, 'calls': [], 'fail_archive': False, 'fail_undo': False, 'gate': None, 'removed': False}

        async def route_api(route):
            req = route.request
            url = urlparse(req.url)
            path = url.path
            if path == f'/api/p/{pid}/sessions' and req.method == 'GET':
                archived = parse_qs(url.query).get('archived') == ['true']
                await route.fulfill(json=[r for r in rows.values() if archived or not r['archived']])
            elif path.startswith(f'/api/p/{pid}/sessions/') and path.rsplit('/', 1)[-1] in rows:
                selected = path.rsplit('/', 1)[-1]
                if req.method == 'PATCH':
                    body = req.post_data_json
                    state['calls'].append((selected, body))
                    key = 'fail_archive' if body['archived'] else 'fail_undo'
                    if state[key]:
                        state[key] = False
                        await route.fulfill(status=500, json={'detail': 'Fixture request failed'})
                        return
                    if not body['archived'] and state['gate']:
                        await state['gate'].wait()
                    rows[selected]['archived'] = body['archived']
                    await route.fulfill(json={'ok': True, 'worktree': {'removed': True, 'branch': 'merged-fixture'} if state['removed'] else None})
                else:
                    snap = copy.deepcopy(snapshot)
                    snap.update(session=rows[selected], live=False, state=None)
                    await route.fulfill(json=snap)
            elif req.method not in ('GET', 'HEAD'):
                await route.fulfill(status=400, json={'detail': 'Unexpected mutation in archive UI check'})
            else:
                await route.continue_()

        await ctx.route('**/api/**', route_api)
        return state

    async def goto(page):
        await page.goto(f'{base}/?project={pid}&session={sid}', wait_until='networkidle')
        await expect(page.locator('.chatcol .topbar').get_by_role('button', name='Archive', exact=True)).to_be_enabled()

    async def archive(page):
        await page.locator('.chatcol .topbar').get_by_role('button', name='Archive', exact=True).click()
        await expect(page.locator('.toast.with-action').last).to_be_visible()
        await expect(page.locator('.toast.with-action').last).to_have_css('opacity', '1')

    async def agents(page):
        if await page.get_by_role('button', name='Agents and menu').count():
            await page.get_by_role('button', name='Agents and menu').filter(visible=True).click()
        await page.get_by_role('button', name='All agents', exact=True).click()

    async def selected(page, selected_id):
        await expect(page).to_have_url(__import__('re').compile('session=' + selected_id))

    async with async_playwright() as pw:
        browser = await pw.webkit.launch(**({'executable_path': args.webkit} if args.webkit else {}))
        ctx = await browser.new_context(viewport={'width': 1440, 'height': 1000})
        state = await setup(ctx)
        page = await ctx.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        await goto(page)
        await page.locator('.composer-input').fill('Keep this unsent reply')
        state['fail_archive'] = True
        await page.locator('.chatcol .topbar').get_by_role('button', name='Archive', exact=True).click()
        await expect(page.locator('.toast.error')).to_be_visible()
        await selected(page, sid)
        await expect(page.locator('.toast.with-action')).to_have_count(0)
        assert not state['rows'][sid]['archived']
        checks.append('failed archive preserves the open conversation and offers no false Undo')

        await archive(page)
        await page.wait_for_timeout(3700)
        undo = page.get_by_role('button', name='Undo', exact=True)
        await expect(undo).to_be_visible()
        state['fail_undo'] = True
        await undo.click()
        await expect(undo).to_be_enabled()
        assert state['rows'][sid]['archived']
        state['gate'] = asyncio.Event()
        count = len(state['calls'])
        await undo.click()
        await expect(page.get_by_role('button', name='Restoring…')).to_be_disabled()
        await page.get_by_role('button', name='Restoring…').evaluate('(el) => { el.click(); el.click(); }')
        await page.wait_for_timeout(200)
        assert len(state['calls']) == count + 1
        state['gate'].set()
        await expect(page.locator('.toast.with-action')).to_have_count(0)
        await selected(page, sid)
        await expect(page.locator('.composer-input')).to_have_value('Keep this unsent reply')
        checks.append('Undo survives the ordinary toast timeout, retries after failure, prevents duplicate requests, and restores the draft')

        await archive(page)
        await agents(page)
        row_b = page.locator('.sb-row').filter(has_text='Second fixture')
        await row_b.locator('.sb-item').click()
        await selected(page, 'fixture-b')
        await page.get_by_role('button', name='Undo', exact=True).click()
        await expect(page.locator('.toast.with-action')).to_have_count(0)
        await selected(page, 'fixture-b')
        assert not state['rows'][sid]['archived']
        checks.append('Undo restores the original session without stealing a newly selected conversation')

        await agents(page)
        row_a = page.locator('.sb-row').filter(has_text='Archive fixture')
        await row_a.hover()
        await row_a.get_by_role('button', name='Archive', exact=True).click()
        await expect(page.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        await page.get_by_role('button', name='Undo', exact=True).click()
        await expect(page.locator('.toast.with-action')).to_have_count(0)
        await agents(page)  # clicking the notification closes the flyout as an outside click
        await expect(row_a).to_be_visible()
        await selected(page, 'fixture-b')
        await row_a.hover()
        await row_a.get_by_role('button', name='Archive', exact=True).click()
        await page.locator('.rail-flyout').get_by_title('Close', exact=True).click()
        await archive(page)
        await expect(page.get_by_role('button', name='Undo', exact=True)).to_have_count(2)
        await page.locator('.toast.with-action').filter(has_text='Archive fixture').get_by_role('button', name='Undo', exact=True).click()
        await expect(page.locator('.toast.with-action')).to_have_count(1)
        assert not state['rows'][sid]['archived'] and state['rows']['fixture-b']['archived']
        await page.get_by_role('button', name='Dismiss notification', exact=True).click()
        await expect(page.locator('.toast.with-action')).to_have_count(0)
        assert state['rows']['fixture-b']['archived']
        checks.append('list Undo remains clickable over its flyout; multiple archives undo independently; dismiss keeps it archived')

        await goto(page)
        state['removed'] = True
        await archive(page)
        await expect(page.locator('.toast.with-action')).to_contain_text('Undo restores the conversation only')
        await page.get_by_role('button', name='Home', exact=True).click()
        await expect(page.locator('.homepage')).to_be_visible()
        await page.get_by_role('button', name='Undo', exact=True).focus()
        await page.keyboard.press('Enter')
        await expect(page.locator('.toast.with-action')).to_have_count(0)
        await expect(page.locator('.homepage')).to_be_visible()
        assert state['calls'][-1] == (sid, {'archived': False})
        checks.append('Undo stays tied to the source project after navigation; removed worktree limitation is shown')
        await ctx.close()

        for pose, (w, h, layout, *insets) in {**duo.POSES, **duo.APP_POSES}.items():
            inset = insets[0] if insets else None
            ctx = await browser.new_context(viewport={'width': w, 'height': h}, is_mobile=True, has_touch=True, device_scale_factor=2)
            state = await setup(ctx, long_name=True)
            page = await ctx.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            if inset:
                await page.route('**/*.css', duo.with_insets(inset))
            await goto(page)
            await expect(page.locator('html')).to_have_attribute('data-layout', layout)
            await archive(page)
            await page.screenshot(path=str(args.shots / (pose + '.png')), animations='disabled')
            # Use the repository's phone audit on the changed UI; unrelated panes are covered by duo-check.py.
            findings = await page.evaluate(duo.AUDIT, [duo.MIN_TAP, inset])
            assert not [f for f in findings if 'toast' in f.get('el', '')], (pose, findings)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), pose
            await page.get_by_role('button', name='Undo', exact=True).tap()
            await selected(page, sid)
            assert not state['rows'][sid]['archived']
            await agents(page)
            await page.locator('.sb-row').filter(has_text='Second fixture').get_by_role('button', name='Archive', exact=True).tap()
            await page.get_by_role('button', name='Undo', exact=True).tap()
            await expect(page.locator('.toast.with-action')).to_have_count(0)
            assert not state['rows']['fixture-b']['archived']
            checks.append(pose + ': both entry points, single-tap Undo, long names, 30px targets and safe-area bounds')
            await ctx.close()

        ctx = await browser.new_context(viewport={'width': 1440, 'height': 1000})
        page = await ctx.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        await goto(page)
        await archive(page)
        archived = duo.get(f'{base}/api/p/{pid}/sessions?archived=true')
        assert next(r for r in archived if r['id'] == sid)['archived']
        await page.screenshot(path=str(args.shots / 'desktop-real-api.png'), animations='disabled')
        await page.get_by_role('button', name='Undo', exact=True).click()
        await expect(page.locator('.toast.with-action')).to_have_count(0)
        await selected(page, sid)
        restored = duo.get(f'{base}/api/p/{pid}/sessions')
        assert not next(r for r in restored if r['id'] == sid)['archived']
        assert duo.get(f'{base}/api/p/{pid}/sessions/{sid}')['items'] == snapshot['items']
        checks.append('real scratch API: archived=true then false, visible again, original transcript unchanged')
        await ctx.close()
        await browser.close()

    report = {'checks': checks, 'errors': errors}
    (args.shots / 'result.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    assert not errors, errors


if __name__ == '__main__':
    asyncio.run(main())
