#!/usr/bin/env python3
"""Check Undo dismissal against a scratch Atrium instance, with all mutations intercepted."""
import argparse
import asyncio
import json
from urllib.parse import urlparse
from urllib.request import urlopen

from playwright.async_api import async_playwright, expect


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:8846')
    parser.add_argument('--webkit')
    args = parser.parse_args()
    base = args.base.rstrip('/')

    def get(path):
        with urlopen(base + path) as response:
            return json.load(response)

    pid = get('/api/projects')[0]['id']
    source = next(s for s in get(f'/api/p/{pid}/sessions') if s['role'] == 'worker')
    sid = source['id']
    snapshot = get(f'/api/p/{pid}/sessions/{sid}')
    state = {'archived': False, 'gate': None, 'fail': False}
    checks, errors = [], []

    async def route_api(route):
        request = route.request
        path = urlparse(request.url).path
        row = {**source, 'name': 'Undo timing fixture', 'live': None, 'archived': state['archived']}
        if path == f'/api/p/{pid}/sessions' and request.method == 'GET':
            await route.fulfill(json=[row] if not state['archived'] or 'archived=true' in request.url else [])
        elif path == f'/api/p/{pid}/sessions/{sid}':
            if request.method == 'PATCH':
                archived = request.post_data_json['archived']
                if not archived:
                    if state['gate']:
                        await state['gate'].wait()
                    if state['fail']:
                        state['fail'] = False
                        await route.fulfill(status=500, json={'detail': 'Retry fixture'})
                        return
                state['archived'] = archived
                await route.fulfill(json={'ok': True, 'worktree': None})
            else:
                await route.fulfill(json={**snapshot, 'session': row, 'live': False, 'state': None})
        elif request.method not in ('GET', 'HEAD'):
            await route.abort()
        else:
            await route.continue_()

    async with async_playwright() as pw:
        browser = await pw.webkit.launch(**({'executable_path': args.webkit} if args.webkit else {}))
        ctx = await browser.new_context(viewport={'width': 1440, 'height': 1000})
        await ctx.route('**/api/**', route_api)
        page = await ctx.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.clock.install()
        notice = page.locator('.toast.with-action')
        undo = page.get_by_role('button', name='Undo', exact=True)

        async def archive():
            state.update(archived=False, gate=None, fail=False)
            await page.goto(f'{base}/?project={pid}&session={sid}', wait_until='networkidle')
            await page.locator('.chatcol .topbar').get_by_role('button', name='Archive', exact=True).click()
            await expect(undo).to_be_visible()
            await page.mouse.move(0, 0)

        await archive()
        await page.clock.fast_forward(5000)
        await expect(undo).to_be_visible()
        await page.clock.fast_forward(1100)
        await expect(notice).to_have_count(0)
        assert state['archived']
        await page.get_by_role('button', name='All agents', exact=True).click()
        await page.locator('.rail-flyout').get_by_title('Show archived', exact=True).click()
        await page.get_by_role('button', name='Unarchive', exact=True).click()
        await expect(page.locator('.sb-item').filter(has_text='Undo timing fixture')).to_be_visible()
        assert not state['archived']
        checks.append('visible at 5s, dismissed after 6s; the archived conversation can still be manually restored')

        await archive()
        await notice.hover()
        await page.clock.fast_forward(7000)
        await expect(undo).to_be_visible()
        await page.mouse.move(0, 0)
        await page.clock.fast_forward(6100)
        await expect(notice).to_have_count(0)
        checks.append('hover pauses dismissal; leaving gives a fresh 6s window')

        await archive()
        await page.keyboard.press('Tab')
        await undo.focus()
        await page.clock.fast_forward(7000)
        await expect(undo).to_be_visible()
        await undo.evaluate('(el) => el.blur()')
        await page.clock.fast_forward(6100)
        await expect(notice).to_have_count(0)
        checks.append('keyboard focus pauses dismissal; blur resumes it')

        await archive()
        state.update(gate=asyncio.Event(), fail=True)
        await undo.click()
        await page.mouse.move(0, 0)
        await page.clock.fast_forward(7000)
        await expect(page.get_by_role('button', name='Restoring…')).to_be_disabled()
        state['gate'].set()
        await expect(undo).to_be_enabled()
        await page.clock.fast_forward(5000)
        await expect(undo).to_be_visible()
        await undo.click()
        await expect(notice).to_have_count(0)
        assert not state['archived']
        checks.append('pending restore survives the timeout; failure gets a fresh window and retry succeeds')
        await browser.close()

    print(json.dumps({'checks': checks, 'errors': errors}, indent=2))
    assert not errors, errors


if __name__ == '__main__':
    asyncio.run(main())
