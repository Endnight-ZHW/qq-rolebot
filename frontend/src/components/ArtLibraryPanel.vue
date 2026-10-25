<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { api } from '../lib/api';
import { core, useAction } from '../lib/state';
import { confirmAction } from '../lib/confirm';
import Overlay from './Overlay.vue';
const { busy, error, action } = useAction();
const rows=ref<any[]>([]), storage=ref(0), search=ref(''), showDeleted=ref(false), editing=ref<any>(null);
const uploadFile=ref<File|null>(null), fileInput=ref<HTMLInputElement|null>(null);
const upload=ref({label:'',kind:'character',persona:'default',usage:''});
const kinds:Record<string,string>={character:'角色人物',background:'背景',decoration:'装饰',prop:'道具',panel:'信息面板',reference:'参考图'};
const visible=computed(()=>rows.value.filter(r=>!!r.deleted===showDeleted.value&&(!search.value||(r.label+' '+r.usage).includes(search.value))));
async function load(){const r=await api<any>('/creation-assets');rows.value=r.items;storage.value=r.stored_bytes;}
function selectFile(e:Event){uploadFile.value=(e.target as HTMLInputElement).files?.[0]||null;if(uploadFile.value&&!upload.value.label)upload.value.label=uploadFile.value.name.replace(/\.[^.]+$/,'').slice(0,80);}
async function add(){if(!uploadFile.value)return;await action(async()=>{const data=new FormData();data.append('file',uploadFile.value!);for(const [k,v] of Object.entries(upload.value))data.append(k,v);await api('/creation-assets','POST',data);uploadFile.value=null;upload.value.label='';upload.value.usage='';if(fileInput.value)fileInput.value.value='';await load();},'素材已加入');}
async function update(row:any,changes:any){await action(async()=>{await api('/creation-assets/'+encodeURIComponent(row.id),'PUT',{expected_revision:row.revision,...changes});await load();},'素材已更新');}
async function save(){const r=editing.value;await action(async()=>{await api('/creation-assets/'+encodeURIComponent(r.id),'PUT',{expected_revision:r.revision,label:r.label,kind:r.kind,persona:r.persona||null,usage:r.usage});editing.value=null;await load();},'素材说明已保存');}
async function remove(row:any){if(!await confirmAction('删除素材',row.builtin?'将从可用素材库移除，可在“已删除”中恢复。已发出的作品不会被撤回。':'将删除这份上传素材的源文件和预览。已发出的作品不会被撤回。','删除'))return;await action(async()=>{await api('/creation-assets/'+encodeURIComponent(row.id)+'?expected_revision='+row.revision,'DELETE');await load();},'素材已删除');}
const image=(row:any)=>'/api/v1/creation-assets/'+encodeURIComponent(row.id)+'/file?v='+row.revision;
onMounted(()=>action(load,''));
</script>
<template>
 <section class="panel">
  <h2>美术素材</h2>
  <p>普通聊天不加载素材库。制作时按需求选择启用素材，人物素材只供绑定的角色使用；停用或删除后，未发送的任务会重新核验。</p>
  <p class="muted">支持 SVG、PNG、JPG、WebP、GIF，单个 8 MiB；SVG 最多 96 KiB。素材库最多 256 个、512 MiB，已用 {{(storage/1048576).toFixed(1)}} MiB。SVG 上传时自动生成预览。</p>
  <form class="asset-upload" @submit.prevent="add">
   <label>素材文件<input ref="fileInput" type="file" accept=".svg,.png,.jpg,.jpeg,.webp,.gif" required @change="selectFile" /></label>
   <label>素材名称<input v-model="upload.label" required maxlength="80" /></label>
   <label>素材类别<select v-model="upload.kind" aria-label="素材类别"><option v-for="(label,key) in kinds" :value="key" :key="key">{{label}}</option></select></label>
   <label>绑定角色<select v-model="upload.persona" aria-label="绑定角色"><option value="">通用素材</option><option v-for="p in core.personas" :value="p.id" :key="p.id">{{p.name}}</option></select></label>
   <label class="wide">素材用途与检索词<textarea v-model="upload.usage" maxlength="500" placeholder="例如：侧躺抱鱼、床铺、午睡；人物特征和适合的场景" /></label>
   <button class="primary" type="submit" :disabled="busy||!uploadFile">{{busy?'处理中…':'加入素材'}}</button>
  </form>
 </section>
 <p v-if="error" role="alert" class="error">{{error}}</p>
 <div class="toolbar"><input v-model="search" aria-label="查找美术素材" placeholder="按名称或用途查找"/><label><input v-model="showDeleted" type="checkbox"/> 已删除</label><button class="secondary" @click="action(load,'')">刷新素材</button></div>
 <div class="asset-grid">
  <article v-for="row in visible" :key="row.id" class="panel asset-card">
   <img v-if="!row.deleted" :src="image(row)" :alt="row.label" loading="lazy" />
   <h3>{{row.label}}</h3><p class="muted">{{kinds[row.kind]}} · {{row.builtin?'内置':'上传'}} · {{row.persona?core.personas.find((p:any)=>p.id===row.persona)?.name||row.persona:'通用'}} · {{row.width}} × {{row.height}}</p>
   <p>{{row.usage||'尚未填写用途'}}</p>
   <template v-if="!row.deleted"><label><input type="checkbox" :checked="row.enabled" :disabled="busy" :aria-label="'启用 '+row.label" @change="update(row,{enabled:($event.target as HTMLInputElement).checked})"/> {{row.enabled?'已启用':'已停用'}}</label>
   <div class="asset-actions"><button class="secondary" @click="editing={...row}">编辑说明</button><a :href="image(row)+'&source=true'" download>下载源文件</a><button class="danger" :disabled="busy" @click="remove(row)">删除素材</button></div></template>
   <button v-else-if="row.builtin" class="secondary" :disabled="busy" @click="update(row,{deleted:false,enabled:true})">恢复素材</button>
   <p v-else class="muted">文件已删除，需要时可重新上传。</p>
  </article>
 </div>
 <p v-if="!visible.length" class="muted">没有符合条件的素材。</p>
 <Overlay v-if="editing" title="编辑美术素材" @close="editing=null">
  <form class="asset-upload" @submit.prevent="save"><label>名称<input v-model="editing.label" required maxlength="80"/></label><label>类别<select v-model="editing.kind" aria-label="类别"><option v-for="(label,key) in kinds" :value="key" :key="key">{{label}}</option></select></label><label>角色<select v-model="editing.persona" aria-label="角色"><option :value="null">通用素材</option><option v-for="p in core.personas" :value="p.id" :key="p.id">{{p.name}}</option></select></label><label class="wide">用途与检索词<textarea v-model="editing.usage" maxlength="500"/></label><button class="primary" :disabled="busy">保存素材说明</button></form>
 </Overlay>
</template>
<style scoped>
.asset-upload{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:20px}.asset-upload label{display:grid;gap:7px}.wide{grid-column:1/-1}.asset-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(245px,1fr));gap:16px}.asset-card{min-width:0}.asset-card img{display:block;width:100%;height:220px;object-fit:contain;background:#edf3f0;border-radius:12px}.asset-card p{overflow-wrap:anywhere}.asset-actions{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-top:14px}.toolbar{margin:20px 0}@media(max-width:650px){.asset-upload{grid-template-columns:1fr}}
</style>
