'use strict';
// Initialize theme synchronously before first paint (CSP-compliant, no inline script in index.html).
(() => {
  let m; try { m = localStorage.getItem('kaponir-theme'); } catch {}
  if (m !== 'light' && m !== 'dark') m = 'auto';
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.themeMode = m;
  document.documentElement.dataset.theme = m === 'auto' ? (dark ? 'dark' : 'light') : m;
})();
// Source-key dictionaries. Only explicitly marked interface strings are translated.
// Document contents, tokens and rule values never pass through this translator.
(async () => {
  const supported = ['ru', 'en'];
  let saved; try { saved=localStorage.getItem('kaponir-language'); } catch {}
  let language=supported.includes(saved)?saved:(navigator.language.startsWith('ru')?'ru':'en');
  try {
    const dictionaries=Object.fromEntries(await Promise.all(supported.map(async code=>{
      const response=await fetch('locales/'+code+'.json');if(!response.ok)throw new Error('Translation file unavailable');return [code,await response.json()];
    })));
    window.tr=(key,args=[])=>String(dictionaries[language][key]??key).replace(/\{\{(\d+)\}\}/g,(_,i)=>String(args[Number(i)]??''));
    window.I18N={get language(){return language;},setLanguage(value){if(!supported.includes(value))return;language=value;document.documentElement.lang=value;try{localStorage.setItem('kaponir-language',value);}catch{}},
      apply(){
        document.documentElement.lang=language;
        for(const element of document.querySelectorAll('[data-i18n]'))element.textContent=tr(element.dataset.i18n);
        for(const attr of ['title','placeholder','aria-label'])for(const element of document.querySelectorAll('[data-i18n-'+attr+']'))element.setAttribute(attr,tr(element.getAttribute('data-i18n-'+attr)));
      }};
    I18N.apply();
    for(const file of ['app.js','panel.js','providers.js','s1.js','worker.js','help.js','review.js'])await new Promise((resolve,reject)=>{const script=document.createElement('script');script.src=file;script.onload=resolve;script.onerror=reject;document.head.append(script);});
    await startApp();
  }catch(error){
    const main=document.getElementById('content');const message=document.createElement('p');message.textContent=language==='ru'?'Не удалось загрузить панель. Проверьте соединение и обновите страницу.':'Unable to load the panel. Check your connection and reload the page.';main.prepend(message);console.error(error);
  }
})();
