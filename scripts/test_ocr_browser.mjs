// Playwright checks against the proxy started by test_ocr_proxy.py --browser-fixture.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve } from "node:path";

const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const config = JSON.parse(await readFile(process.argv[2], "utf8"));
const output = resolve(process.argv[3] || "target/ocr-browser");
await mkdir(output, {recursive:true});
const sha = bytes => createHash("sha256").update(bytes).digest("hex");
const upstream = () => fetch(config.upstream + "/__fixture_digest").then(response=>response.json());
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_EXECUTABLE || undefined,
  headless: true,
});
const context = await browser.newContext({viewport:{width:1366,height:768},locale:"ru-RU"});
const errors = [], originalReads = [], areaReads = [];
const page = await context.newPage();
page.on("pageerror", error=>errors.push(error.message));
page.on("request", request=>{
  if (request.url().includes("/ocr/images/") && request.url().endsWith("/original")) originalReads.push(request.url());
  if (request.url().includes("/ocr/images/") && /\/area-\d+$/.test(request.url())) areaReads.push(request.url());
});
await page.addInitScript(({token})=>{
  if (!localStorage.getItem("kaponir-language")) localStorage.setItem("kaponir-language","ru");
  if (!localStorage.getItem("kaponir-theme")) localStorage.setItem("kaponir-theme","light");
  sessionStorage.setItem("kaponir-token",token);
},config);

async function capture(file) {
  await page.screenshot({path:resolve(output,file),animations:"disabled"});
}
async function hit(path, payload, session) {
  const response = await context.request.post(config.url + path,{
    data:payload,headers:{Authorization:"Bearer " + config.token,"x-kaponir-session":session},
  });
  return response;
}
const model = "fixture-chat";
const fixture = await readFile(new URL("../crates/kaponir-proxy/assets/ocr-example.png",import.meta.url));
const sizeLabel = "Размер у прокси · " + [16,20]
  .map(offset=>fixture.readUInt32BE(offset).toLocaleString("ru")).join("×") + " px";
const photo = {type:"image_url",image_url:{url:"data:image/png;base64," + fixture.toString("base64")}};
const user = {role:"user",content:[{type:"text",text:"Проверь конфигурацию"},photo]};

try {
  await page.goto(config.url + "/ui/#ocr");
  await page.getByRole("checkbox",{name:"Защищать изображения перед отправкой"}).waitFor();
  await page.getByText("Защита выключена. Поставщик может получить исходную картинку.").waitFor();
  if (config.install_ocr) {
    const options = page.locator("#ocr-options > summary");
    await options.click();
    const limit = page.locator("#ocr-settings input[name=max_mib]");
    const originalLimit = await limit.inputValue();
    await limit.fill("17");
    await page.getByRole("button",{name:"Установить RapidOCR",exact:true}).click();
    await page.getByText("Скачиваем и проверяем RapidOCR…",{exact:true}).waitFor();
    await page.getByText("RapidOCR установлен.",{exact:true}).waitFor({timeout:120000});
    await page.locator("#ocr-engine[data-state=ready]").waitFor();
    assert.equal(await limit.inputValue(),"17");
    await limit.fill(originalLimit);
    await options.click();
  }
  const setup = page.getByText("Как установить RapidOCR",{exact:true});
  await setup.click();
  await page.getByText("docker compose up -d --build",{exact:true}).waitFor();
  await page.getByText("kaponir ocr setup",{exact:true}).waitFor();
  await setup.click();
  const extra = page.locator("#ocr-options > summary");
  await extra.click();
  const urlMask = page.getByLabel("Маска для URL");
  assert.equal(await urlMask.inputValue(),"full");
  await urlMask.selectOption("partial");
  await extra.click();
  const node = page.getByLabel("Узел обработки");
  await node.selectOption("worker");
  await page.getByText(/Подтвердите адрес получателя и нажмите/).waitFor();
  await page.getByRole("checkbox",{name:"Защищать изображения перед отправкой"}).check();
  const save = page.getByRole("button",{name:"Сохранить изменения"});
  assert(await save.isDisabled());
  await page.getByRole("checkbox",{name:/Разрешить передачу исходных пикселей/}).check();
  await save.click();
  await page.locator("#ocr-engine[data-state=ready]").waitFor();
  assert.match(await page.locator("#ocr-engine").textContent(),/RapidOCR/);
  await node.selectOption("proxy");
  await save.click();
  await page.getByText("Настройки сохранены",{exact:true}).waitFor();
  await page.reload();
  await page.waitForFunction(()=>document.querySelector("#ocr-enabled").checked);
  await page.waitForFunction(()=>document.querySelector("#ocr-url-mask").value==="partial");
  assert.equal(new URL(page.url()).hash,"#ocr");
  await capture("01-nastroyki.png");
  await page.getByRole("link",{name:"Проверить изображение"}).press("Enter");
  await page.getByRole("heading",{name:"Проверка изображения"}).waitFor();
  await page.goBack();
  await page.getByRole("heading",{name:"Защита изображений"}).waitFor();
  await page.getByRole("link",{name:"Проверить изображение"}).click();
  const openCode = page.getByText("Сравнение с OpenCode",{exact:true});
  await openCode.click();
  await page.getByText("В контрольном тесте OpenCode уменьшил PNG с 5000×1320 до 1250×330. RapidOCR полностью закрыл 13 из 13 размеченных значений.",{exact:true}).waitFor();
  await openCode.click();
  const callsBeforeLab = (await upstream()).calls;
  await page.getByRole("button",{name:"Пример с двумя ключами"}).click();
  await page.waitForFunction(()=>document.querySelector("#ocr-file").files[0]?.name==="values.yaml.png");
  const run = page.getByRole("button",{name:"Запустить проверку"});
  await run.press("Enter");
  await page.getByRole("heading",{name:"Закрыто 2 области"}).waitFor();
  await page.getByText(/Распознавание · .* мс/).waitFor();
  await page.getByText(sizeLabel,{exact:true}).waitFor();
  assert.equal((await upstream()).calls,callsBeforeLab);
  const area = page.locator(".ocr-region").first();
  await area.press("Enter");
  assert.equal(await area.getAttribute("aria-pressed"),"true");
  await page.getByRole("button",{name:"Увеличить"}).click();
  await page.getByRole("button",{name:"Вписать"}).click();
  const downloaded = page.waitForEvent("download");
  await page.getByRole("link",{name:"Скачать обработанное изображение"}).click();
  const download = await downloaded;
  const clean = await readFile(await download.path());
  assert.deepEqual(clean.subarray(0,8),Buffer.from([137,80,78,71,13,10,26,10]));
  await capture("02-laboratoriya.png");
  await page.getByRole("link",{name:"К настройкам OCR"}).click();
  await page.getByRole("heading",{name:"Защита изображений"}).waitFor();

  const answer = await hit("/v1/chat/completions",{model,messages:[user]},"ocr-browser");
  assert.equal(answer.status(),200);
  const wire = await upstream();
  assert.deepEqual(wire.images,[sha(clean)]);
  const continuation = [
    user,
    {role:"assistant",content:"",tool_calls:[{type:"function",id:"file-1",function:{name:"read",arguments:"{}"}}]},
    {role:"tool",tool_call_id:"file-1",content:[photo]},
  ];
  assert.equal((await hit("/v1/chat/completions",{model,messages:continuation},"ocr-browser")).status(),200);
  assert.deepEqual((await upstream()).images,[sha(clean),sha(clean)]);
  await page.getByRole("link",{name:"Сессии",exact:true}).click();
  const session = page.locator("#tab-events").getByRole("button",{name:"Открыть переписку"});
  await session.first().click();
  const dialogue = page.locator("#session-dialog");
  await dialogue.getByText("Проверь конфигурацию",{exact:true}).waitFor();
  const card = dialogue.locator(".conversation-question .ocr-attachment");
  await card.waitFor();
  assert.equal(await card.count(),1);
  assert.equal(await card.locator("strong").textContent(),"Закрыто 2 области");
  await card.locator("img").waitFor();
  await page.waitForFunction(()=>document.querySelector(".conversation-question .ocr-thumbnail img")?.naturalWidth > 0);
  await capture("03-perepiska.png");
  const inspectorResponse = page.waitForResponse(response=>response.url().endsWith("/sent") && response.status()===200);
  await card.press("Enter");
  const inspector = page.locator("#ocr-image-dialog");
  await inspector.getByRole("heading",{name:"Отправлено поставщику"}).waitFor();
  await inspector.getByText(sizeLabel,{exact:true}).waitFor();
  const previewURL = (await inspectorResponse).url();
  await page.waitForFunction(()=>document.querySelector("#ocr-image-dialog img")?.naturalWidth > 0);
  const previewBytes = await context.request.get(previewURL,{headers:{Authorization:"Bearer " + config.token}});
  assert.equal(sha(await previewBytes.body()),wire.images[0]);
  assert.equal(await inspector.getByRole("button",{name:"Показать оригинал"}).isDisabled(),true);
  assert.equal(originalReads.length,0);
  await inspector.locator(".ocr-marker").first().click();
  await page.waitForFunction(()=>document.querySelector("#ocr-image-dialog .form-status")?.textContent === "Оригинал не сохранялся");
  assert.equal(areaReads.length,0);
  await inspector.getByRole("button",{name:"Закрыть"}).click();
  assert.equal(await card.evaluate(node=>node===document.activeElement),true);
  await dialogue.getByText("Результат инструмента",{exact:true}).first().click();
  await dialogue.locator(".conversation-tool .ocr-attachment").first().waitFor();
  await dialogue.getByText("Журнал запросов",{exact:false}).first().click();
  await dialogue.getByRole("button",{name:"Изображения",exact:true}).last().click();
  await page.locator("#inspector-dialog .ocr-attachment").first().waitFor();
  assert.equal(await page.locator("#inspector-dialog .ocr-attachment").count(),2);
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");

  // Read the saved image and settings from a fresh process.
  if (config.fixture_pid) {
    const currentRestart = (await upstream()).restarts;
    process.kill(config.fixture_pid,"SIGHUP");
    for (let i = 0; i < 80 && (await upstream()).restarts === currentRestart; i++)
      await new Promise(resolve=>setTimeout(resolve,100));
    assert.equal((await upstream()).restarts,currentRestart+1);
    await page.goto(config.url + "/ui/#ocr");
    await page.waitForFunction(()=>document.querySelector("#ocr-enabled").checked);
    await page.getByRole("link",{name:"Сессии",exact:true}).click();
    await session.first().click();
    await card.waitFor();
    assert.equal(await card.count(),1);
    await page.waitForFunction(()=>document.querySelector(".conversation-question .ocr-thumbnail img")?.naturalWidth > 0);
    await page.keyboard.press("Escape");
  }
  assert.equal(originalReads.length,0);
  const beforeFailure = (await upstream()).calls;
  const bad = {type:"image_url",image_url:{url:"data:image/png;base64,AAAAAA=="}};
  const failure = await hit("/v1/chat/completions",{model,messages:[{
    role:"user",content:[{type:"text",text:"Повреждённый пример"},bad,photo],
  }]},"ocr-failure");
  assert.equal(failure.status(),422);
  assert.equal((await upstream()).calls,beforeFailure);
  await page.getByRole("button",{name:"Обновить",exact:true}).click();
  await session.first().click();
  await dialogue.getByText("Поставщику ничего не отправлено",{exact:true}).waitFor();
  await capture("04-otpravka-ostanovlena.png");
  await page.keyboard.press("Escape");

  await page.getByRole("radio",{name:"English"}).check({force:true});
  await page.getByRole("radio",{name:"Dark theme"}).check({force:true});
  await page.goto(config.url + "/ui/#ocr");
  await page.getByRole("heading",{name:"Image protection"}).waitFor();
  await page.getByText(/Ready · RapidOCR · PP-OCRv6 Small/).waitFor();
  assert.equal(await page.locator("html").getAttribute("data-theme"),"dark");
  await capture("05-settings-dark-en.png");
  await page.getByRole("link",{name:"Test an image"}).click();
  await page.getByText("Compare with OpenCode",{exact:true}).waitFor();
  await page.getByRole("button",{name:"Example with two keys"}).click();
  await page.getByRole("button",{name:"Run check"}).click();
  await page.getByRole("heading",{name:"2 regions covered"}).waitFor();
  await capture("06-lab-dark-en.png");
  // With storage opted in, regular conversation rendering still fetches only the protected image.
  const settingsURL = config.url + "/api/v1/ui/ocr/settings";
  const auth = {Authorization:"Bearer " + config.token};
  const saved = await (await context.request.get(settingsURL,{headers:auth})).json();
  const updated = await context.request.put(settingsURL,{headers:auth,data:{...saved.settings,save_originals:true}});
  assert.equal(updated.status(),200);
  assert.equal((await hit("/v1/chat/completions",{model,messages:[user]},"ocr-audit-opt-in")).status(),200);
  await page.getByRole("link",{name:"Sessions",exact:true}).click();
  await page.locator("#tab-events").getByRole("button",{name:"Open conversation"}).first().click();
  await card.waitFor();
  await dialogue.getByText("Request log",{exact:false}).first().click();
  await dialogue.getByRole("button",{name:"Images",exact:true}).last().click();
  await page.locator("#inspector-dialog .ocr-attachment").first().click();
  await inspector.getByRole("heading",{name:"Sent to provider"}).waitFor();
  assert.equal(originalReads.length,0);
  assert.equal(areaReads.length,0);
  const firstMark = inspector.locator(".ocr-marker").first();
  await firstMark.click();
  await inspector.getByText("Selected region revealed",{exact:true}).waitFor();
  await page.waitForFunction(()=>document.querySelector("#ocr-image-dialog .ocr-revealed")?.naturalWidth > 0);
  assert.equal(areaReads.length,1);
  assert.equal(originalReads.length,0);
  await firstMark.click();
  await inspector.locator(".ocr-revealed").waitFor({state:"detached"});
  const nextRegion = inspector.locator(".ocr-region").nth(1);
  await nextRegion.press("Enter");
  await inspector.getByText("Selected region revealed",{exact:true}).waitFor();
  assert.equal(areaReads.length,2);
  assert.notEqual(areaReads[0],areaReads[1]);
  assert.equal(originalReads.length,0);
  await inspector.getByRole("button",{name:"Show original"}).click();
  await inspector.getByRole("heading",{name:"Original",exact:true}).waitFor();
  assert.equal(originalReads.length,1);
  const original = await context.request.get(originalReads[0],{headers:auth});
  assert.equal(sha(await original.body()),sha(fixture));
  assert.deepEqual(errors,[]);
  console.log("1366×768 · Russian/light and English/dark · no browser exceptions · upstream and inspector PNG hashes match");
} finally {
  await browser.close();
}
