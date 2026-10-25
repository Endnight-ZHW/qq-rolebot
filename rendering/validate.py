"""Fresh isolated process/image: never import the generated source."""
import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path
import subprocess
import sys
from PIL import Image
sys.path.insert(0,'/opt')
from creation_contract import OUTPUT_LIMITS, output_names, validate_spec,timeline_layout

root=Path('/input')
spec=validate_spec(json.loads((root/'spec.json').read_text()))
rows=[]
for name in output_names(spec):
    path=root/name
    if path.is_symlink() or not path.is_file(): raise ValueError('缺少输出文件')
    kind=path.suffix[1:]
    if not 0<path.stat().st_size<=OUTPUT_LIMITS[kind]: raise ValueError('输出文件超过限额')
    row={'name':name,'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    if kind=='mp4':
        value=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],timeout=15))
        streams=value['streams']
        if len(streams)!=1 or streams[0].get('codec_name')!='h264' or streams[0].get('pix_fmt')!='yuv420p':
            raise ValueError('视频必须为无声 H.264/yuv420p')
        st=streams[0];duration=float(value['format']['duration'])
        if not math.isfinite(duration) or not 0<duration<=20.001:raise ValueError('视频时长超限')
        if st['width']!=spec['width'] or st['height']!=spec['height'] or abs(duration-spec['duration'])>1/spec['fps']+.1:
            raise ValueError('视频尺寸或时长不匹配')
        if Fraction(st['avg_frame_rate'])!=spec['fps'] or int(st.get('nb_frames',0))!=math.ceil(spec['duration']*spec['fps']):
            raise ValueError('视频帧率或帧数不匹配')
        subprocess.run(['ffmpeg','-v','error','-nostdin','-i',str(path),'-f','null','-'],check=True,timeout=25)
        row.update(width=st['width'],height=st['height'],duration=duration,frames=int(st.get('nb_frames',0)),mime='video/mp4')
    else:
        with Image.open(path) as im:
            expected=timeline_layout(spec)[1] if name=='timeline.png' else (spec['width'],spec['height'])
            if im.format!=kind.upper() or im.size!=expected: raise ValueError('图片格式或尺寸不匹配')
            count=getattr(im,'n_frames',1)
            if (kind=='png' and count!=1) or count>math.ceil(spec['duration']*spec['fps'])+1 and kind=='gif':
                raise ValueError('图片帧数与制作清单不匹配')
            total=0
            for i in range(count):
                im.seek(i);im.load();milliseconds=im.info.get('duration',0);total+=milliseconds
                if kind=='gif' and milliseconds<math.floor(1000/spec['fps']/10)*10:raise ValueError('GIF 帧率超限')
            if kind=='gif' and (not 0<total<=8000 or abs(total/1000-spec['duration'])>1/spec['fps']+.11):
                raise ValueError('GIF 时长无效')
            row.update(width=im.width,height=im.height,frames=count,duration=total/1000,mime=Image.MIME[im.format])
    rows.append(row)
print(json.dumps({'artifacts':rows}))
