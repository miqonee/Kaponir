'use strict';
const WORKER={data:null,busy:false,timer:null,signature:null};
const workerStates={queued:'В очереди',running:'Выполняется',paused:'Приостановлено',succeeded:'Готово',failed:'Ошибка',cancelled:'Отменено'};
function renderWorkerModel(){
  const data=WORKER.data;if(!data?.configured)return;
  const model=data.models[$('#worker-model').value];
  $('#worker-model-status').textContent=tr(model.downloaded?'Веса загружены':'Веса ещё не загружены')+' · '+tr(data.runtime_ready?'Runtime готов':'Runtime ещё не установлен');
}
function renderWorker(){
  const data=WORKER.data;$('#worker-controls').hidden=!data?.configured;
  if(!data?.configured){
    $('#worker-state').replaceChildren(h('p',{},tr('Worker не подключён. Запустите его отдельно и задайте serve.worker_url или KAPONIR_WORKER_URL для прокси.')),h('pre',{class:'mono'},'kaponir worker serve\nKAPONIR_WORKER_URL=http://127.0.0.1:8788 kaponir serve'));
    $('#worker-jobs').replaceChildren();return;
  }
  $('#worker-state').replaceChildren(h('p',{},tr('Worker доступен · потоков OCR: {{0}}',[data.threads])),
    ...(!data.image_support||!data.pdf_support?[h('p',{class:'hint'},tr('Для изображений нужен Pillow, для PDF — pypdfium2. В Compose worker они уже включены.')),h('pre',{class:'mono'},'python3 -m pip install Pillow pypdfium2')]:[]));
  $('#worker-root').textContent=tr('Каталог на машине worker: {{0}}',[data.input_root]);
  renderWorkerModel();
  for(const id of ['worker-download','worker-submit'])$('#'+id).disabled=WORKER.busy;
  const rows=(data.jobs||[]).map(job=>h('tr',{},h('td',{class:'mono'},job.id.slice(0,8)),
    h('td',{},job.kind==='download'?tr('Загрузка модели')+' '+job.model:job.source,h('div',{class:'hint'},job.kind==='scan'?tr(job.mode==='xiaomi'?'Поиск кандидатов Xiaomi + локальный поиск':'Локальный поиск · OCR для сканов'):'')),
    h('td',{},tr(workerStates[job.state]||job.state),job.control?h('small',{},' · '+tr('Остановка запрошена')):null),
    h('td',{},job.kind==='download'?(job.state==='succeeded'?tr('Проверено и готово'):Math.round(job.progress/1048576)+' / '+Math.round(job.progress_total/1048576)+' MiB'):job.completed+' / '+job.total,
      h('div',{class:'hint'},job.current||''),job.candidate_page?h('div',{class:'hint'},tr('Xiaomi: страница {{0}}',[job.candidate_page])):null,...(job.errors||[]).map(e=>h('div',{class:'hint'},(e.file||'')+' '+e.type+': '+e.message)),
      ...(job.warnings||[]).map(w=>h('div',{class:'hint'},w.code==='ungrounded_candidates'?tr('Не подтверждены текстом: {{0}}',[w.count]):'S1: '+w.s1_status))),
    h('td',{},...(['queued','running','paused'].includes(job.state)?[
      h('button',{type:'button',class:'secondary',disabled:WORKER.busy,onclick:()=>workerAction('/jobs/'+job.id+'/'+(job.state==='paused'?'resume':'pause'),{})},tr(job.state==='paused'?'Продолжить':'Пауза')),
      h('button',{type:'button',class:'secondary',disabled:WORKER.busy,onclick:()=>workerAction('/jobs/'+job.id+'/cancel',{})},tr('Отменить задание'))]:[]))));
  $('#worker-jobs').replaceChildren(rows.length?h('div',{class:'table-wrap'},h('table',{},h('thead',{},h('tr',{},...['ID','Источник','Состояние','Прогресс','Действия'].map(s=>h('th',{},tr(s))))),h('tbody',{},...rows))):h('p',{},tr('Очередь пуста')));
}
async function loadWorker(){
  clearTimeout(WORKER.timer);
  try{const data=await api('/api/v1/ui/worker');const signature=I18N.language+JSON.stringify(data);WORKER.data=data;if(signature!==WORKER.signature){WORKER.signature=signature;renderWorker();}}
  catch(e){WORKER.signature=null;$('#worker-state').textContent=e.message;$('#worker-controls').hidden=true;}
  if(location.hash==='#worker')WORKER.timer=setTimeout(()=>{if(location.hash==='#worker'&&!document.hidden)loadWorker();},2500);
}
async function workerAction(path,body){
  if(WORKER.busy)return;WORKER.busy=true;renderWorker();
  try{await api('/api/v1/ui/worker'+path,{method:'POST',body});await loadWorker();}
  catch(e){fail(e);}finally{WORKER.busy=false;renderWorker();}
}
$('#worker-refresh').addEventListener('click',loadWorker);
$('#worker-model').addEventListener('change',renderWorkerModel);
$('#worker-download').addEventListener('click',()=>workerAction('/models/download',{model:$('#worker-model').value}));
$('#worker-submit').addEventListener('click',()=>workerAction('/jobs',{source:$('#worker-source').value.trim()||'.',model:$('#worker-model').value,use_s1:$('#worker-s1').checked,mode:$('#worker-mode').value}));
for(const button of document.querySelectorAll('#tab-worker [data-go]'))button.addEventListener('click',()=>showTab(button.dataset.go));
Object.assign(TABS,{worker:loadWorker});

document.addEventListener('visibilitychange',()=>{if(!document.hidden&&location.hash==='#worker')loadWorker();});
