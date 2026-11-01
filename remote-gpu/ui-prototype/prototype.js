'use strict';
const $=id=>document.getElementById(id), server=document.body.dataset.endpoint==='server';
let started=false,incoming=false,capturing=false,returning=false,virtual=false,timer,toastTimer;
function toast(text){$('toast').textContent=text;$('toast').classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('show'),3400)}
function log(text){const line=document.createElement('div'),time=document.createElement('em');time.textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});line.append(time,document.createTextNode(text));$('logs').append(line);$('logs').scrollTop=$('logs').scrollHeight;$('logLatest').textContent=text}
function resolution(){return ($('resolution')?.value||'1280x720').split('x').map(Number)}
function fps(){return parseInt($('fps')?.value||'30',10)}
function setStatus(text,active=false){$('status').lastElementChild.textContent=text;$('status').classList.toggle('live',active)}
function render(){
 $('startBtn').disabled=started;$('stopBtn').disabled=!started;$('simulateBtn').disabled=!started;
 $('inputPreview').classList.toggle('video-active',server?incoming:started);
 $('outputPreview').classList.toggle('video-active',server?capturing:returning);
 $('inputFps').textContent=(server?incoming:started)?fps():'—';
 $('outputFps').textContent=returning?fps():'—';$('rtt').textContent=(server?incoming:started)?'38':'—';
 $('bandwidth').textContent=server?(incoming?(returning?'3.7 / 3.8':'0 / 3.8'):'— / —'):(started?(returning?'3.8 / 3.7':'3.8 / 0'):'— / —');
 $('bandwidth').parentElement.classList.add('band');
 if(server){
  setStatus(!started?'尚未监听':!incoming?'正在监听 · 等待本地接入（演示）':returning?'接收中 · 结果回传中（演示）':'已收到画面 · 等待结果回传（演示）',started);
  $('inputFoot').textContent=incoming?($('inputVcam').checked?'收到原画 · 已提供给 Camera #2（演示）':'收到原画 · 输入虚拟摄像头未开启'):'等待本地接入';
  $('outputFoot').textContent=returning?'正在采集并回传（演示）':capturing?'结果预览中 · 尚未回传':'尚未采集 / 尚未回传';
  $('returnBtn').textContent=returning?'停止回传':'开始回传';
 }else{
  setStatus(!started?'尚未连接':returning?'推送中 · 已收到回传（演示）':'推送中 · 等待服务器回传（演示）',started);
  $('inputFoot').textContent=started?'原画推送中（演示）':'尚未采集';
  $('outputFoot').textContent=returning?(virtual?'收到结果 · 虚拟摄像头输出中（演示）':'收到结果 · 虚拟摄像头未开启'):virtual?'等待回传 · 虚拟摄像头显示等待画面':'尚未收到结果';
  $('vcamBtn').textContent=virtual?'关闭虚拟摄像头':'开启虚拟摄像头';$('vcamBadge').textContent=virtual?'已开启 · 演示':'未开启';$('vcamBadge').classList.toggle('ready',virtual);
 }
 draw();
}
function draw(){const [w,h]=resolution();document.querySelectorAll('.dimensions').forEach(e=>e.textContent=`${w} × ${h}`);for(const name of ['input','output']){const c=$(name+'Canvas');c.width=w;c.height=h;const g=c.getContext('2d'),s=Math.min(w,h);const grad=g.createLinearGradient(0,0,w,h);grad.addColorStop(0,name==='input'?'#263e62':'#284d62');grad.addColorStop(1,'#27636d');g.fillStyle=grad;g.fillRect(0,0,w,h);g.strokeStyle='#d5e7ff18';g.lineWidth=1;const grid=Math.max(w,h)/16;for(let x=0;x<w;x+=grid){g.beginPath();g.moveTo(x,0);g.lineTo(x,h);g.stroke()}for(let y=0;y<h;y+=grid){g.beginPath();g.moveTo(0,y);g.lineTo(w,y);g.stroke()}g.strokeStyle='#a4b9d755';g.lineWidth=s/400;g.beginPath();g.arc(w/2,h/2,s*.27,0,Math.PI*2);g.stroke();g.textAlign='center';g.fillStyle='#e1edff';g.font=`500 ${s*.048}px 'Segoe UI',sans-serif`;g.fillText('FaceLive Remote',w/2,h/2-s*.025);g.fillStyle='#a9bfd9';g.font=`${s*.027}px 'Microsoft YaHei',sans-serif`;g.fillText(name==='input'?'原始输入 · 演示':'结果采集 · 演示（未换脸）',w/2,h/2+s*.055);['#c5d0dc','#c8c383','#80b8b6','#76a48f','#928ab4','#b47f91','#6f88b6'].forEach((color,i)=>{g.fillStyle=color;g.fillRect(w/2-s*.24+i*s*.48/7,h/2+s*.15,s*.48/7,s*.025)});g.fillStyle='#8ba5c7';g.font=`${s*.024}px 'Segoe UI',sans-serif`;g.fillText(`${w} × ${h}  /  ${fps()} FPS`,w/2,h-s*.07)}}
$('showKey').onclick=()=>{const hide=$('key').type==='password';$('key').type=hide?'text':'password';$('showKey').textContent=hide?'隐藏':'显示'};
 var configCollapsed=false;
 function collapseConfig(value){configCollapsed=value;$('serverConfigFields').classList.toggle('hidden',value);$('serverConfigSummary').classList.toggle('hidden',!value);$('toggleConfig').textContent=value?'展开':'收起';$('serverConfigSummary').textContent=($('publicIp').value||'尚未确认地址')+':'+$('publicPort').value+' · Key 已准备';}
 if(server)$('toggleConfig').onclick=()=>collapseConfig(!configCollapsed);

$('startBtn').onclick=()=>{
 if(server){if(!$('publicIp').value.trim()){toast('请先识别或填写服务器对外地址');$('publicIp').focus();return}if(!$('listenPort').value||!$('publicPort').value||!$('listenPort').checkValidity()||!$('publicPort').checkValidity()){toast('请填写 1–65535 范围内的端口');return}started=true;collapseConfig(true);render();log('演示：服务器开始接收，等待本地端接入；结果尚未回传');toast('可点击预览区的「模拟本地接入」继续体验');return}
 if(!$('address').value.trim()||!$('key').value.trim()){toast('请填写服务器地址和 Key，或点击「导入配置」体验示例');return}
 if(!$('fingerprint').value)log('演示：手填连接参数；真实连接仍需完成服务器证书校验')
 started=true;render();log('演示：本地原画开始推送，等待服务器回传');toast('推送和回传独立。点击「模拟结果回传」查看接收状态');
};
$('stopBtn').onclick=()=>{started=incoming=returning=false;clearTimeout(timer);render();log(server?'演示：接收与网络回传已停止；结果预览可保留':'演示：停止推送和网络连接，虚拟摄像头开关保持独立')};
$('simulateBtn').onclick=()=>{if(!started)return;if(server){incoming=true;render();log('演示：已接收本地画面，等待采集换脸结果');toast('现在可选择结果窗口，先预览再开始回传')}else{returning=true;render();log('演示：收到服务器结果；测试图案不代表实际换脸效果');toast('已展示模拟回传画面，未建立真实网络连接')}};
if(server){
 $('detectBtn').onclick=()=>{$('publicIp').value='203.0.113.10';$('ipHint').textContent='演示地址已填入，未进行真实 IP 检测；正式版仍需核对平台公网地址。';toast('仅填入文档示例 IP，不代表你的服务器 IP');log('演示：已填入公网地址示例')};
 $('copyConfig').onclick=async()=>{if(!$('publicIp').value.trim()){toast('确认对外地址后才能生成可连接的配置');return}const config='RGPU-DEMO-ONLY.'+btoa(JSON.stringify({address:`https://${$('publicIp').value}:${$('publicPort').value}`,key:'DEMO-NONFUNCTIONAL',certificate:'DEMO-NONFUNCTIONAL'}));try{await navigator.clipboard.writeText(config);toast('已复制演示配置，不可用于真实连接')}catch{toast('浏览器未允许复制；此处仅演示复制配置交互')}$('configHint').textContent=started?'演示配置已复制 · 服务器正在监听':'演示配置已复制 · 还需点击「启动接收」';log('演示：配置已准备，包含地址、Key 与证书校验信息')};
 function sourceChanged(){const type=$('resultType').value;$('windowFields').classList.toggle('hidden',type!=='window');$('cameraFields').classList.toggle('hidden',type!=='camera');$('loopFields').classList.toggle('hidden',type!=='loop');capturing=returning=false;$('outputTag').textContent=type==='window'?'窗口采集':type==='camera'?'结果摄像头':'原画回环';render()}
 $('resultType').onchange=sourceChanged;
 $('resultWindow').onchange=$('resultCamera').onchange=()=>{capturing=returning=false;render()};
 $('refreshWindows').onclick=()=>toast('原型窗口列表为示例；正式版在服务器本机列出窗口');
 function validSource(){const type=$('resultType').value;if(type==='window'&&!$('resultWindow').value){toast('请先选择换脸软件的输出窗口');return false}if(type==='camera'&&!$('resultCamera').value){toast('请先选择结果虚拟摄像头');return false}if(type==='loop'&&!incoming){toast('原画回环需要先收到本地画面');return false}return true}
 $('captureBtn').onclick=()=>{if(!validSource())return;capturing=true;const type=$('resultType').value;$('outputTag').textContent=type==='window'?'所选窗口':type==='camera'?'结果摄像头':'原画回环';render();log('演示：结果来源预览已开启，尚未新增网络回传');toast('仅预览所选结果来源；点击「开始回传」才发送')};
 $('returnBtn').onclick=()=>{if(returning){returning=false;render();log('演示：回传已停止，接收保持运行');return}if(!started||!incoming){toast('先启动接收，并通过「模拟本地接入」收到输入画面');return}if(!validSource())return;capturing=returning=true;render();log('演示：正在采集所选来源并回传，接收画面保持运行')};
 $('inputVcam').onchange=render;
}else{
 $('importBtn').onclick=()=>{$('address').value='https://203.0.113.10:8766';$('key').value='demo_key_server_7h28x';$('fingerprint').value='已导入示例证书指纹（演示）';toast('已填入演示配置，未读取剪贴板或连接服务器');log('演示：已导入地址、Key 和服务器证书校验信息')};
 $('vcamBtn').onclick=()=>{virtual=!virtual;render();log(virtual?'演示：虚拟摄像头已开启；无回传时显示等待画面':'演示：虚拟摄像头已关闭，网络推送保持当前状态')};
 $('resolution').onchange=$('fps').onchange=render;$('source').onchange=()=>{$('inputTag').textContent=$('source').value};
}
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-view]').forEach(x=>{const on=x===b;x.classList.toggle('active',on);x.setAttribute('aria-pressed',String(on))});$('previews').classList.toggle('single',b.dataset.view==='single')});
$('expandBtn').onclick=()=>$('previewPanel').classList.toggle('fullscreen');document.addEventListener('keydown',e=>{if(e.key==='Escape')$('previewPanel').classList.remove('fullscreen')});render();
