/** Read-only local UI validation. Every API response is an explicitly synthetic fixture. */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
const base=process.env.RESPONSIVE_BASE_URL??'http://127.0.0.1:3108';
if(!['127.0.0.1','localhost'].includes(new URL(base).hostname))throw Error('Responsive fixtures require a local frontend');
const output=process.env.RESPONSIVE_ARTIFACT_DIR??'/tmp/ai-trade-responsive';fs.mkdirSync(path.join(output,'screenshots'),{recursive:true});
const fixture=JSON.parse(fs.readFileSync(new URL('../tests/fixtures/monitoring.json',import.meta.url))).fixtures;
const widths=[[1440,900],[1280,800],[1024,768],[768,1024],[430,932],[390,844],[360,800]];
const routes=[['overview','/'],['phase15','/phase15'],['strategies','/nifty'],['positions','/positions'],['trades','/trades'],['research','/nifty/research'],['history','/history'],['snapshot','/history/1'],['shadow','/shadow'],['shadow-detail','/shadow/7'],['system','/system'],['commodity','/commodity-acts'],['crude','/commodity-acts/CRUDEOILM'],['gas','/commodity-acts/NATGASMINI']];
const results={notice:'SYNTHETIC UI FIXTURES ONLY; no production, broker, database or actual fill evidence',renders:[],states:[],navigation:[]};
const save=()=>fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(results,null,2));
async function prepare(context,theme='dark',state='normal'){
 const data=structuredClone(fixture),requests=[],unexpected=[];
 if(state==='stale')for(const s of data['/api/phase15/summary'].strategies)s.latest_timestamp='2026-10-22T06:30:00Z';
 if(['inside','below'].includes(state))for(const s of data['/api/phase15/summary'].strategies){if(!s.orb_range)continue;Object.assign(s.orb_range,{current_future_price:state==='inside'?25650:25600,breakout_direction:state==='inside'?null:'BEAR',status:state==='inside'?'NO_SETUP':'WATCH',decision_reason:null});}
 await context.addInitScript(({theme})=>{localStorage.setItem('ai-trade-theme',theme);const Real=Date;window.Date=class extends Real{constructor(...args){super(...(args.length?args:['2026-10-23T06:30:00Z']));}static now(){return Real.parse('2026-10-23T06:30:00Z');}};},{theme});
 await context.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());const api=url.pathname.indexOf('/api/');
  if(api>=0){const key=url.pathname.slice(api);requests.push({path:key,method:req.method()});
   if(req.method()!=='GET'||!Object.hasOwn(data,key)){unexpected.push({path:key,method:req.method()});return route.abort();}
   if(state==='commodity-unavailable'&&key.startsWith('/api/commodity-acts/'))return route.fulfill({status:404,headers:{'access-control-allow-origin':'*'},contentType:'application/json',body:'{}'});
   if(state==='unavailable'&&key.startsWith('/api/phase15'))return route.fulfill({status:503,headers:{'access-control-allow-origin':'*'},contentType:'application/json',body:'{}'});
   return route.fulfill({headers:{'access-control-allow-origin':'*','cache-control':'no-store'},contentType:'application/json',body:JSON.stringify(data[key])});
  }
  if(url.origin!==new URL(base).origin){unexpected.push({destination:url.origin});return route.abort();}
  return route.continue();
 });return{requests,unexpected};
}
async function measure(page){return page.evaluate(()=>{
 const visible=e=>{if(e.closest('details:not([open])')&&!e.closest('summary'))return false;return !!(e.getBoundingClientRect().width&&e.getBoundingClientRect().height)&&getComputedStyle(e).visibility!=='hidden';};
 const heading=[...document.querySelectorAll('h2')].find(e=>e.textContent==='OPEN POSITIONS');
 return{width:innerWidth,documentWidth:document.documentElement.scrollWidth,height:innerHeight,documentHeight:document.documentElement.scrollHeight,positionsTop:heading?Math.round(heading.getBoundingClientRect().top+scrollY):null,theme:document.documentElement.dataset.theme,
   clippedNumbers:[...document.querySelectorAll('.trading-summary .metric-value,.position-prices dd')].filter(e=>visible(e)&&e.scrollWidth>e.clientWidth+2).map(e=>e.textContent),
   scrollAreas:[...document.querySelectorAll('.table-wrap')].filter(e=>visible(e)&&e.scrollWidth>e.clientWidth+2).map(e=>({width:e.clientWidth,scrollWidth:e.scrollWidth})),
   strategyStates:document.querySelectorAll('.strategy-pulse a').length};
 });}
const quick=process.env.RESPONSIVE_QUICK==='1';
for(const theme of ['dark','light'])for(const [width,height]of (quick?widths.filter(([w])=>[1440,390].includes(w)):widths)){
 const browser=await chromium.launch({headless:true});
 try{for(const [name,route]of (quick?routes.filter(([n])=>['overview','phase15','strategies','trades','research','system'].includes(n)):routes)){
  const context=await browser.newContext({viewport:{width,height},colorScheme:theme,reducedMotion:'reduce'});const audit=await prepare(context,theme);const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+route,{waitUntil:'networkidle'});await page.waitForTimeout(100);
  const row={name,route,...await measure(page),errors,unexpected:audit.unexpected};results.renders.push(row);
  await page.screenshot({path:path.join(output,`screenshots/${name}-${width}-${theme}.png`)});
  if(['overview','phase15','strategies','positions','trades'].includes(name)&&[1440,390].includes(width))await page.screenshot({path:path.join(output,`screenshots/${name}-${width}-${theme}-full.png`),fullPage:true});
  save();await context.close();
 }}finally{await browser.close();}
 console.log(`Rendered ${theme} ${width}: ${results.renders.length} checks`);
}
for(const state of ['normal','inside','below','stale','unavailable','commodity-unavailable']){
 const browser=await chromium.launch({headless:true});const context=await browser.newContext({viewport:{width:390,height:844},hasTouch:true});const audit=await prepare(context,'dark',state);const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+(state==='inside'||state==='below'?'/nifty':'/'),{waitUntil:'networkidle'});
 if(state==='inside'||state==='below'){await page.getByRole('tabpanel').scrollIntoViewIfNeeded();}
 if(state==='commodity-unavailable'){
  await page.getByText('Cross-market portfolio · NIFTY + Commodity',{exact:true}).click();
  await page.getByText(/Partial or missing records/).waitFor();
  if(!await page.getByRole('article',{name:'ACTS V2 position UI-FIXTURE-ACTS-OPEN'}).count())throw Error('Commodity failure hid NIFTY exposure');
  for(const instrument of ['CRUDEOILM','NATGASMINI']){
   await page.goto(base+'/commodity-acts/'+instrument,{waitUntil:'networkidle'});
   await page.getByText('COMMODITY RESEARCH UNAVAILABLE',{exact:true}).waitFor();
   await page.screenshot({path:path.join(output,`screenshots/missing-${instrument}-390.png`)});
  }
  await page.goto(base+'/',{waitUntil:'networkidle'});
 }
 await page.screenshot({path:path.join(output,`screenshots/state-${state}-390.png`)});
 const body=await page.locator('body').innerText();
 results.states.push({state,...await measure(page),errors,unexpected:audit.unexpected,hasUnresolved:body.includes('unresolved exposure'),hasUnavailable:body.includes('UNAVAILABLE'),hasLosingMark:body.includes('520.00'),hasProfitableMark:body.includes('52.00')});
 if(state==='normal'){
  await page.getByRole('button',{name:'More navigation'}).click();const dialog=page.getByRole('dialog',{name:'More'});await dialog.waitFor({state:'visible'});await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
  results.navigation.push({moreClosesWithEscape:true,focusReturned:await page.getByRole('button',{name:'More navigation'}).evaluate(e=>e===document.activeElement)});
  await page.getByRole('button',{name:'Switch to light theme'}).click();if(await page.locator('html').getAttribute('data-theme')!=='light')throw Error('Theme switch failed');
  await page.goto(base+'/trades',{waitUntil:'networkidle'});await page.getByText('Filters · Today',{exact:true}).click();await page.getByLabel('Strategy',{exact:true}).selectOption('ORB_15');
  await page.screenshot({path:path.join(output,'screenshots/mobile-filters-light.png')});
  const expired=page.getByRole('row').filter({hasText:'CONTRACT EXPIRED / EXIT PRICE UNAVAILABLE'}).first();if(!await expired.count())throw Error('Unpriced expiry evidence lost');const expiredText=await expired.innerText();if(!expiredText.includes('N/A')||expiredText.includes('₹0.00'))throw Error('Unpriced expiry fabricated a value');
 }
 save();await browser.close();
}
const failures=results.renders.filter(r=>r.documentWidth>r.width+1||r.errors.length||r.unexpected.length||r.clippedNumbers.length);
if(failures.length){console.error(JSON.stringify(failures,null,2));throw Error(`${failures.length} responsive failures`);}
for(const r of results.renders.filter(r=>['/','/phase15'].includes(r.route)&&r.width===390))if(r.positionsTop==null||r.positionsTop>900)throw Error('Exposure not within initial monitoring budget');
if(results.states.some(r=>r.errors.length||r.unexpected.length))throw Error('State regression');
console.log(`PASS: ${results.renders.length} route/viewport/theme combinations, representative states, navigation and themes.`);
