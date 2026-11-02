'use strict';
const PROVIDERS = {kind:'openai', loaded:false, busy:false, connections:[], active:{}, drafts:{}, models:{}, selected:{}, routes:{}, result:null, removing:false};
function providerProfile(){return PROVIDERS.connections.find(p=>p.id===PROVIDERS.active[PROVIDERS.kind]);}
function providerKey(){return PROVIDERS.kind+':'+(PROVIDERS.active[PROVIDERS.kind]||'new');}
function profileDraft(p){return {name:p?.name||'',base_url:p?.base_url||(PROVIDERS.kind==='openai'?'https://api.openai.com/v1':'https://api.anthropic.com'),api_key:'',enabled:p?.enabled??true,pool:p?.pool??false,retry_on_429:p?.retry_on_429??false,all_models:p?.all_models??false,auto_models:p?.auto_models??true,model_text:(p?.models||[]).join('\n')};}
function providerDraft(){return PROVIDERS.drafts[providerKey()] ||= profileDraft(providerProfile());}
function providerIsDirty(){const p=providerProfile(),d=providerDraft();if(!p)return false;const before=profileDraft(p);return Object.keys(before).some(k=>d[k]!==before[k]);}
function updateProviderDraftNote(){const note=$('#provider-draft-note');if(note)note.hidden=!providerIsDirty();}
function chooseProvider(id){const current=providerProfile(),newKey=PROVIDERS.kind+':new';if(!id&&current&&!PROVIDERS.drafts[newKey])PROVIDERS.drafts[newKey]={...profileDraft(current),name:'',enabled:true,all_models:false,auto_models:true,pool:false,retry_on_429:false};PROVIDERS.active[PROVIDERS.kind]=id;PROVIDERS.result=null;PROVIDERS.removing=false;renderProviderConnection();}
async function loadProviders(){
  if(!PROVIDERS.loaded){
    $('#connection-cards').replaceChildren(h('p',{role:'status'},tr('Загрузка поставщиков…')));
    try{const data=await api('/api/v1/connections');PROVIDERS.connections=data.connections;for(const kind of ['openai','anthropic'])PROVIDERS.active[kind]=data.connections.find(p=>p.kind===kind)?.id||null;PROVIDERS.loaded=true;}
    catch(error){$('#connection-cards').replaceChildren(h('div',{class:'panel connection-card'},h('p',{role:'alert'},error.message),h('button',{onclick:loadProviders},tr('Повторить'))));return;}
  }
  renderProviderConnection();
}
function renderProviderConnection(){
  if(!PROVIDERS.loaded)return;
  const P=PROVIDERS,kind=P.kind,k=providerKey(),d=providerDraft(),profile=providerProfile(),busy=P.busy;
  const protocol=h('div',{class:'seg provider-protocol','aria-label':tr('Формат API')},...['openai','anthropic'].map(kind=>h('button',{type:'button',disabled:busy,'aria-pressed':String(P.kind===kind),onclick:()=>{P.kind=kind;P.result=null;P.removing=false;renderProviderConnection();}},kind==='openai'?'OpenAI-compatible':'Anthropic')));
  const connections=P.connections.filter(p=>p.kind===kind);
  const manager=h('article',{class:'panel connection-manager'},h('div',{class:'connections-heading'},h('div',{},h('h2',{},tr('Ваши подключения')),h('p',{class:'hint'},tr('Несколько ключей: общий адрес или отдельное подключение.'))),h('button',{class:'secondary',disabled:busy,onclick:()=>chooseProvider(null)},h('span',{'aria-hidden':'true'},'+'),tr('Добавить ключ'))),protocol,
    h('div',{class:'connection-list'},...connections.map(p=>h('button',{class:'connection-tile',disabled:busy,'aria-pressed':String(profile?.id===p.id),onclick:()=>chooseProvider(p.id)},h('strong',{},p.name),h('span',{class:'connection-host'},new URL(p.base_url).host),h('span',{class:'connection-mode'},tr(!p.enabled?'Отключено':p.pool?'В общем адресе':'Отдельный адрес')))),...(!connections.length?[h('p',{class:'hint'},tr('Добавьте первый ключ. Каждый ключ хранится отдельно и получает свой адрес.'))]:[])));
  const clearResult=()=>{P.result=null;$('#provider-result').replaceChildren();updateProviderDraftNote();};
  const resetModels=()=>{d.model_text='';d.all_models=false;P.models[k]=[];const all=$('#provider-all-models');if(all)all.checked=false;};
  const preset=h('select',{id:'provider-preset',onchange:e=>{if(e.target.value){d.base_url=e.target.value;d.api_key='';resetModels();P.result=null;renderProviderConnection();}}},h('option',{value:''},tr('Свой поставщик')),...(kind==='openai'?[
    ['OpenAI','https://api.openai.com/v1'],['OpenRouter','https://openrouter.ai/api/v1'],['DeepSeek','https://api.deepseek.com/v1'],
  ]:[['Anthropic','https://api.anthropic.com']]).map(([name,url])=>h('option',{value:url,selected:d.base_url.replace(/\/$/,'')===url||d.base_url===url.replace(/\/v1$/,'')},name)));
  const name=h('input',{id:'provider-name',required:true,maxlength:120,value:d.name,placeholder:tr('Например: Основной ключ или Команда разработки'),oninput:e=>{d.name=e.target.value;clearResult();}});
  const url=h('input',{id:'provider-url',type:'url',required:true,value:d.base_url,placeholder:'https://api.example.com/v1',spellcheck:'false',autocomplete:'off',oninput:e=>{d.base_url=e.target.value;resetModels();clearResult();preset.value='';if($('#provider-models-input'))$('#provider-models-input').value='';}});
  const key=h('input',{id:'provider-key',type:'password',value:d.api_key,autocomplete:'new-password',spellcheck:'false',placeholder:tr(profile?.key_saved?'Сохранён. Введите новый для замены':'Вставьте ключ поставщика'),oninput:e=>{d.api_key=e.target.value;clearResult();}});
  const mode=h('select',{id:'provider-mode','aria-describedby':'provider-mode-help',onchange:e=>{d.pool=e.target.value==='pool';P.result=null;renderProviderConnection();}},h('option',{value:'direct',selected:!d.pool},tr('Только отдельный адрес')),h('option',{value:'pool',selected:d.pool},tr('Общий и отдельный адрес')));
  const enabled=h('input',{type:'checkbox',checked:d.enabled,onchange:e=>{d.enabled=e.target.checked;clearResult();}});
  const modelsInput=h('textarea',{id:'provider-models-input',spellcheck:'false',placeholder:'vendor/model-a\nvendor/model-b',oninput:e=>{d.model_text=e.target.value;clearResult();}},d.model_text);
  const result=h('div',{id:'provider-result',role:'status','aria-live':'polite',class:'provider-result'});
  if(P.result)result.append(h('p',{class:P.result.error?'err':'ok'},tr(P.result.messageKey)+(P.result.more?' '+tr('Поставщик вернул первую страницу моделей.'):'') ));
  const form=h('form',{id:'provider-form',onsubmit:e=>{e.preventDefault();providerAction('save');}},
    h('p',{id:'provider-draft-note',class:'draft-note',hidden:!providerIsDirty()},tr('Изменения ещё не сохранены. Приложения используют прежнее подключение.')),
    h('fieldset',{disabled:busy},h('legend',{class:'sr-only'},tr('Настройки поставщика')),
      h('label',{for:'provider-name'},tr('Название подключения'),name),h('label',{for:'provider-preset'},tr('Поставщик'),preset),
      h('label',{for:'provider-url'},tr('Адрес API поставщика'),url),h('p',{class:'hint'},tr('Можно указать адрес с /v1. Для локального сервиса разрешён HTTP на localhost.')),
      h('label',{for:'provider-key'},tr('API-ключ поставщика'),h('div',{class:'key-input'},key,h('button',{type:'button',class:'icon-button','aria-label':tr('Показать ключ'),title:tr('Показать ключ'),onclick:e=>{const visible=key.type==='text';key.type=visible?'password':'text';e.currentTarget.setAttribute('aria-label',tr(visible?'Показать ключ':'Скрыть ключ'));e.currentTarget.title=tr(visible?'Показать ключ':'Скрыть ключ');}},icon('eye-off')))),h('p',{class:'key-note'},icon('lock'),tr('Ключ шифруется в vault. После сохранения он не возвращается в браузер.')),
      h('details',{class:'provider-advanced',open:d.pool||!d.auto_models||!d.enabled},h('summary',{},tr('Маршрутизация и модели'),icon('chevron')),
        h('label',{for:'provider-mode'},tr('Выбор подключения для запросов'),mode),
        h('p',{id:'provider-mode-help',class:'hint'},tr(d.pool?'Скопируйте общий адрес справа в своё приложение. Kaponir закрепляет запросы одного диалога и модели за одним ключом. По умолчанию ошибки возвращаются приложению без повторов. Отдельный адрес всегда использует выбранный ключ. Балансы не объединяются.':'Скопируйте отдельный адрес справа в своё приложение. Все запросы по нему используют только этот ключ. Подходит, если хотите сами выбирать поставщика. Общий адрес этот ключ не использует.')),
        ...(d.pool?[h('label',{class:'provider-check'},h('input',{id:'provider-retry-429',type:'checkbox',checked:d.retry_on_429,onchange:e=>{d.retry_on_429=e.target.checked;clearResult();}}),tr('Переключать ключ при HTTP 429')),h('p',{class:'hint'},tr('Необязательно. Повторяет запрос с другим ключом на том же адресе API, если опция включена у обоих подключений. Включайте только для взаимозаменяемых аккаунтов и запросов с полной историей: состояние и кэш поставщика между ключами могут не переноситься.'))]:[]),
        h('label',{class:'provider-check'},h('input',{id:'provider-auto-models',type:'checkbox',checked:d.auto_models,onchange:e=>{d.auto_models=e.target.checked;d.all_models=false;P.result=null;renderProviderConnection();}}),tr('Получать модели автоматически')),
        h('p',{class:'hint'},tr('Список загрузится при сохранении из /v1/models и будет обновляться при использовании, не чаще раза в пять минут.')),
        ...(!d.auto_models?[h('label',{for:'provider-models-input'},tr('Модели для общего адреса'),modelsInput),h('p',{class:'hint'},tr('По одному ID на строку. Проверка заполнит список; оставьте модели, которые хотите объединить.'))]:[]),
        ...(profile?.all_models&&!d.auto_models?[h('label',{class:'provider-check'},h('input',{id:'provider-all-models',type:'checkbox',checked:d.all_models,onchange:e=>{d.all_models=e.target.checked;clearResult();}}),tr('Все модели — совместимость с прежней настройкой'))]:[]),
        h('label',{class:'provider-check'},enabled,tr('Подключение включено'))),
      h('div',{class:'provider-actions'},h('button',{type:'button',onclick:()=>{if(form.reportValidity())providerAction('test');}},tr(busy?'Подождите…':'Проверить и получить модели')),h('button',{type:'submit',class:'primary'},tr(busy?'Подождите…':'Сохранить подключение')))),result);
  const left=h('article',{class:'panel provider-card'},h('div',{class:'provider-heading'},h('span',{class:'step-number'},'1'),h('div',{},h('h2',{},tr(profile?'Настройки подключения':'Новое подключение')),h('p',{},tr('Ключ и модели от сервиса, которым вы пользуетесь.')))),form,h('p',{class:'hint'},tr('Проверка запрашивает только список моделей. Сообщения и документы не отправляются.')));
  if(profile)left.append(P.removing?h('div',{class:'remove-connection'},h('p',{},tr('Удалить подключение «{{0}}»? Его адрес перестанет принимать новые запросы.',[profile.name])),h('button',{disabled:busy,onclick:()=>providerAction('delete')},tr('Удалить подключение')),h('button',{disabled:busy,onclick:()=>{P.removing=false;renderProviderConnection();}},tr('Отмена'))):h('button',{class:'text-button',disabled:busy,onclick:()=>{P.removing=true;renderProviderConnection();}},tr('Удалить подключение')));
  const right=h('article',{class:'panel provider-card client-card'},h('div',{class:'provider-heading'},h('span',{class:'step-number'},'2'),h('div',{},h('h2',{},tr('Подключите приложение')),h('p',{},tr('Скопируйте параметры Kaponir в свой клиент.')))));
  if(profile){
    right.append(h('div',{class:'provider-saved '+(profile.enabled?'ready':'')},icon('shield'),tr(profile.enabled?'Поставщик сохранён':'Подключение отключено')),h('h3',{},tr('Отдельный адрес')),copyCode(location.origin+profile.endpoint+(kind==='openai'?'/v1':'')),h('p',{class:'hint'},tr('Этот адрес всегда использует только выбранное подключение.')));
    if(profile.pool)right.append(h('h3',{},tr('Общий адрес')),copyCode(location.origin+(kind==='openai'?'/v1':'')),h('p',{class:'hint'},tr('Объединённый список моделей доступен через GET /v1/models.')));
    right.append(h('h3',{},tr('Ключ для приложения')),copyCode(info.api_token_required?'YOUR_KAPONIR_TOKEN':'kaponir-local'),h('p',{class:'hint'},tr(info.api_token_required?'Укажите токен доступа Kaponir, заданный в serve.api_token. Ключ поставщика клиенту не нужен.':'Локальный доступ без проверки токена. Если клиент требует API-ключ, укажите kaponir-local.')));
    if(profile.pool)right.append(h('div',{class:'seg endpoint-mode'},...['direct','pool'].map(route=>h('button',{type:'button','aria-pressed':String((P.routes[k]||'direct')===route),onclick:()=>{P.routes[k]=route;renderProviderConnection();}},tr(route==='direct'?'Отдельный адрес':'Общий адрес')))));
    right.append(h('label',{for:'provider-model'},tr('Модель'),h('input',{id:'provider-model',list:'provider-model-list',value:P.selected[k]||'',placeholder:tr('ID модели из списка или от поставщика'),autocomplete:'off',oninput:e=>{P.selected[k]=e.target.value;renderProviderExample();}})),h('datalist',{id:'provider-model-list'},...(P.models[k]||profile.models).map(id=>h('option',{value:id}))),h('div',{id:'provider-example'}));
    if(profile.auto_models){
      right.append(h('p',{class:profile.models_error?'catalog-status err':'catalog-status'},profile.models_error?tr('Не удалось обновить модели. Показан последний успешный список.')+' '+tr(profile.models_error):tr('Модели обновлены: {{0}}',[profile.models_checked_at?new Date(profile.models_checked_at*1000).toLocaleString(I18N.language):'—']),h('button',{class:'text-button',type:'button',disabled:busy,onclick:()=>providerAction('refresh')},tr('Обновить модели'))));
    }
    const available=P.models[k]||profile.models;
    if(available.length)right.append(h('h3',{},tr('Доступные модели: {{0}}',[available.length])),h('div',{class:'model-list'},...available.map(id=>h('button',{type:'button',title:id,'aria-pressed':String(P.selected[k]===id),onclick:()=>{P.selected[k]=id;renderProviderConnection();}},id))));
  }else right.append(h('div',{class:'provider-empty'},icon('plug'),h('h3',{},tr('Новый ключ — свой адрес')),h('p',{},tr('После сохранения появится отдельный endpoint. При включении общего режима ключ также войдёт в пул моделей.'))));
  $('#connection-cards').replaceChildren(manager,left,right);renderProviderExample();
}
function renderProviderExample(){
  const target=$('#provider-example'),p=providerProfile();if(!target||!p)return;
  const P=PROVIDERS,k=providerKey(),kind=P.kind,model=P.selected[k]||'MODEL_ID',base=location.origin+(P.routes[k]==='pool'&&p.pool?'':p.endpoint);
  const key=info.api_token_required?'YOUR_KAPONIR_TOKEN':'kaponir-local';
  const example=kind==='openai'?`from openai import OpenAI\n\nclient = OpenAI(\n    base_url=${JSON.stringify(base+'/v1')},\n    api_key=${JSON.stringify(key)},\n)\nresponse = client.chat.completions.create(\n    model=${JSON.stringify(model)},\n    messages=[{"role": "user", "content": "Hello"}],\n)\nprint(response.choices[0].message.content)`:`from anthropic import Anthropic\n\nclient = Anthropic(\n    base_url=${JSON.stringify(base)},\n    auth_token=${JSON.stringify(key)},\n    api_key=None,\n)\nresponse = client.messages.create(\n    model=${JSON.stringify(model)},\n    max_tokens=256,\n    messages=[{"role": "user", "content": "Hello"}],\n)\nprint(response.content)`;
  target.replaceChildren(h('h3',{},tr('Пример Python')),copyCode(example),h('p',{class:'hint'},tr('Клиент выбирает модель в каждом запросе. Kaponir сохраняет её ID.')),h('p',{class:'hint'},tr('Дополнительные заголовки сессии не нужны. Сохраните обычные настройки агента, изменив только адрес API и ключ доступа. Kaponir скрывает данные в запросах и восстанавливает их в ответах.')));
}
async function providerAction(action){
  const P=PROVIDERS;if(P.busy)return;
  const k=providerKey(),kind=P.kind,p=providerProfile(),d=providerDraft(),body={kind,id:p?.id,...d,api_key:d.api_key||null};
  if(d.auto_models) delete body.models; else body.models=d.model_text.split(/\n/).map(s=>s.trim()).filter(Boolean);
  delete body.model_text;
  P.busy=true;P.result=null;renderProviderConnection();
  try{
    if(action==='refresh'){
      const result=await api('/api/v1/connections/'+p.id+'/models/refresh',{method:'POST'});
      P.connections[P.connections.findIndex(c=>c.id===p.id)]=result;P.models[k]=result.models;
      // Keep edits in the form; only the automatic model snapshot changes.
      d.model_text=result.models.join('\n');P.result={error:!!result.models_error,messageKey:result.models_error||'Список моделей обновлён'};
    }else if(action==='delete'){
      await api('/api/v1/connections/'+p.id,{method:'DELETE'});P.connections=P.connections.filter(c=>c.id!==p.id);delete P.drafts[k];delete P.models[k];delete P.selected[k];delete P.routes[k];P.active[kind]=P.connections.find(c=>c.kind===kind)?.id||null;P.removing=false;P.result={messageKey:'Подключение удалено'};
    }else{
      const result=await api(action==='test'?'/api/v1/connections/test':'/api/v1/connections'+(p?'/'+p.id:''),{method:action==='test'||!p?'POST':'PUT',body});
      if(action==='test'){P.models[k]=result.models;d.model_text=result.models.join('\n');d.all_models=false;P.result={messageKey:result.models.length?'Соединение проверено. Модели получены.':'Соединение проверено. Список моделей пуст.',more:result.has_more};}
      else{const index=P.connections.findIndex(c=>c.id===result.id);if(index>=0)P.connections[index]=result;else P.connections.push(result);P.active[kind]=result.id;const next=providerKey();P.drafts[next]=profileDraft(result);P.models[next]=result.models;if(next!==k){P.selected[next]=P.selected[k];delete P.drafts[k];delete P.models[k];delete P.selected[k];}P.result={error:!!result.models_error,messageKey:result.models_error?'Подключение сохранено, но модели не загрузились. Проверьте доступ или задайте список вручную.':'Сохранено. Новые запросы используют этого поставщика.'};}
    }
  }catch(error){P.result={error:true,messageKey:error.messageKey||error.message};}
  finally{P.busy=false;renderProviderConnection();}
}

window.addEventListener('kaponir-settings-imported', () => {
  Object.assign(PROVIDERS, {loaded:false,connections:[],active:{},drafts:{},models:{},selected:{},routes:{},result:null,removing:false});
});
