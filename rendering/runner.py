"""Runs ONLY inside a disposable container; generated code is untrusted."""
import asyncio
import functools
import http.server
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import sys
from urllib.parse import urlsplit

from PIL import Image,ImageDraw,ImageChops,ImageStat
sys.path.insert(0,'/opt')
from creation_contract import validate_spec, validate_data,timeline_layout

ROOT=Path('/work')


async def frames(spec, data):
    if spec['runtime']=='python':
        module_spec=importlib.util.spec_from_file_location('artwork', ROOT/'main.py')
        module=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(module)
        assets={p.name:str(p) for p in (ROOT/'assets').iterdir()}
        count=spec['pages'] if spec['format']=='png' else math.ceil(spec['duration']*spec['fps'])
        for i in range(count):
            im=module.render(0 if spec['format']=='png' else i/spec['fps'],spec['width'],spec['height'],data,assets,i if spec['format']=='png' else 0)
            if not isinstance(im,Image.Image) or im.size!=(spec['width'],spec['height']):
                raise ValueError('render 必须返回指定尺寸的 PIL.Image')
            yield im.convert('RGB')
        return
    class Static(http.server.SimpleHTTPRequestHandler):
        def log_message(self,*args): pass
        def translate_path(self,path):
            relative=urlsplit(path).path.lstrip('/')
            base=Path('/opt/www') if relative.startswith('vendor/') else ROOT
            target=(base/relative).resolve()
            if not target.is_relative_to(base): return '/nonexistent'
            return str(target)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Static,directory=str(ROOT)))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    from playwright.async_api import async_playwright
    origin=f'http://127.0.0.1:{server.server_port}'
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(chromium_sandbox=True,headless=True,
            args=['--use-angle=swiftshader','--enable-unsafe-swiftshader','--disable-dev-shm-usage'])
        try:
            context=await browser.new_context(viewport={'width':spec['width'],'height':spec['height']},
                device_scale_factor=1,service_workers='block',accept_downloads=False)
            await context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(origin+'/') else route.abort())
            page=await context.new_page()
            await page.add_init_script('window.ART_DATA='+json.dumps(data,ensure_ascii=False)+';')
            await page.goto(origin+'/index.html',wait_until='load',timeout=15000)
            await page.evaluate('async()=>{await document.fonts.ready;await window.ready;}')
            count=spec['pages'] if spec['format']=='png' else math.ceil(spec['duration']*spec['fps'])
            for i in range(count):
                await page.evaluate('async([t,p,animated])=>{if(window.renderFrame) await window.renderFrame(t,p);else if(animated) throw Error("需要 renderFrame(t,page)");}',
                    [0 if spec['format']=='png' else i/spec['fps'],i if spec['format']=='png' else 0,spec['format']!='png'])
                raw=await page.screenshot(type='png',animations='disabled',timeout=15000)
                yield Image.open(io.BytesIO(raw)).convert('RGB')
        finally:
            await browser.close();server.shutdown()


async def main():
    spec=validate_spec(json.loads((ROOT/'spec.json').read_text()))
    data=validate_data(json.loads((ROOT/'data.json').read_text()))
    out=ROOT/'out';out.mkdir(exist_ok=True)
    if spec['format']=='png':
        i=0
        async for im in frames(spec,data):
            im.save(out/f'page-{i}.png');im.close();i+=1
        return
    # Only one decoded frame is live at a time. The intermediate MP4 is bounded.
    command=['ffmpeg','-v','error','-nostdin','-y','-f','rawvideo','-pix_fmt','rgb24','-s',
             f"{spec['width']}x{spec['height']}",'-r',str(spec['fps']),'-i','pipe:0','-an',
             '-c:v','libx264','-threads','2','-preset','veryfast','-pix_fmt','yuv420p',
             '-movflags','+faststart','-fs','25165824',str(out/'video.mp4')]
    process=subprocess.Popen(command,stdin=subprocess.PIPE)
    tile,size=timeline_layout(spec)
    timeline=Image.new('RGB',size,'#ffffff');pen=ImageDraw.Draw(timeline)
    selected={};previous=None
    try:
        first=True;index=0;count=math.ceil(spec['duration']*spec['fps'])
        async for im in frames(spec,data):
            if first:im.save(out/'poster.png');first=False
            if index==count//2:im.save(out/'proof-mid.png')
            if index==count-1:im.save(out/'proof-end.png')
            # Keep one thumbnail per time bin, choosing its largest actual frame
            # change. This catches short blinks and avoids sampling a periodic
            # animation only at identical start/middle/end phases.
            small=im.resize((96,72)).convert('L')
            score=sum(ImageStat.Stat(ImageChops.difference(small,previous)).mean) if previous is not None else 0
            if previous is not None:previous.close()
            previous=small
            slot=min(11,index*12//count)
            if slot not in selected or score>selected[slot]:
                selected[slot]=score;x=(slot%4)*tile[0];y=(slot//4)*(tile[1]+20)
                thumb=im.resize(tile,Image.Resampling.LANCZOS);timeline.paste(thumb,(x,y));thumb.close()
                pen.rectangle((x,y+tile[1],x+tile[0]-1,y+tile[1]+19),fill='white')
                pen.text((x+4,y+tile[1]+3),f't={index/spec["fps"]:.2f}s',fill='black')
            process.stdin.write(im.tobytes());im.close()
            index+=1
        process.stdin.close()
        if process.wait(timeout=20):raise ValueError('视频编码失败')
        timeline.save(out/'timeline.png')
    finally:
        if process.poll() is None:process.kill();process.wait()
        timeline.close()
        if previous is not None:previous.close()
    if spec['format']=='gif':
        # Two bounded passes avoid retaining all frames in a filter graph.
        subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-i',str(out/'video.mp4'),
                        '-vf','palettegen','-frames:v','1',str(ROOT/'palette.png')],check=True,timeout=20)
        subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-i',str(out/'video.mp4'),
                        '-i',str(ROOT/'palette.png'),'-lavfi','paletteuse','-loop','0',
                        '-fs','8388608',str(out/'animation.gif')],check=True,timeout=20)
        (out/'video.mp4').unlink()


if __name__=='__main__':
    asyncio.run(main())
