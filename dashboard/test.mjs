import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html=fs.readFileSync(new URL("index.html",import.meta.url),"utf8");
const scripts=[...html.matchAll(/<script(?: [^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
assert.equal(scripts.length,2);
assert(!/<(?:script|link|img)[^>]*(?:src|href)\s*=/.test(html),"external resource unexpectedly present");
assert(/name="viewport"/.test(html));
for(const width of [780,540])assert(html.includes("@media(max-width:"+width+"px)"));
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.attributes={};this.dataset={};this.style={};this.listeners={};this.textContent="";this.value="";}
 get firstElementChild(){return this.children[0]}
 append(...nodes){for(const n of nodes){n.parent=this;this.children.push(n)}}
 replaceChildren(...nodes){this.children=[];this.append(...nodes)}
 setAttribute(k,v){this.attributes[k]=v}
 addEventListener(k,v){this.listeners[k]=v}
 fire(k){this.listeners[k]()}
 replaceWith(node){if(this.parent){const i=this.parent.children.indexOf(this);this.parent.children[i]=node;node.parent=this.parent}}
}
const elems=Object.fromEntries([...html.matchAll(/<([a-z][\w-]*)[^>]* id="([^"]+)"/g)].map(([_,tag,id])=>[id,new Element(tag)]));
const nextParent=new Element("section");nextParent.append(elems["next-area"]);
const m={createElement:t=>new Element(t),createElementNS:(_,t)=>new Element(t),getElementById:id=>elems[id]};
elems["split"].value="dev";elems["family"].value="all";elems["action"].value="rewrite_refactor";
elems["fixed-bar"].append(new Element("span"));elems["oracle-bar"].append(new Element("span"));
elems["embedded-data"].textContent=scripts[0];
const ctx={document:m};vm.createContext(ctx);vm.runInContext(scripts[1],ctx);
assert.equal(elems["bars"].children.length,15);
assert.equal(elems["fixed-score"].textContent,"1.87");
assert.equal(elems["oracle-score"].textContent,"1.87");
assert.equal(elems["stats-groups"].textContent,31);
assert.equal(elems["recipe-metrics"].children.length,3);
const clicked=elems["bars"].children[1];clicked.fire("click");
assert.equal(clicked.attributes["aria-pressed"],"true");
elems["family"].value="B";elems["family"].fire("change");
assert.equal(elems["bars"].children.length,9);
assert.match(elems["bars"].children[0].attributes["aria-label"],/^B/);
elems["split"].value="train";elems["split"].fire("change");
assert.equal(elems["bars"].children.length,8);
elems["family"].value="all";elems["split"].value="all";
elems["action"].value="strash_only";elems["action"].fire("change");
assert.equal(elems["bars"].children.length,31);
assert.equal(elems["filter-count"].textContent,"31개 그룹");
const embedded=JSON.parse(scripts[0]);
const expectedNext=embedded.next?.summary.state;
if(expectedNext) assert.equal(elems["next-stage-label"].textContent,"NEXT · "+(expectedNext==="VERIFIED_DEVELOPMENT_ONLY"?"VERIFIED":"BLOCKED"));
else assert.equal(elems["next-stage-label"].textContent,"");
if(expectedNext==="VERIFIED_DEVELOPMENT_ONLY"){
 const panel=nextParent.children[0];
 assert.equal(panel.children[2].children.length,4);
 assert.deepEqual(panel.children[2].children.map(n=>n.children[1].textContent),[31,31,31,12]);
 assert.match(panel.children[4].children[0].textContent,/없었습니다/);
 const select=panel.children[5].children[0];
 select.value="all";select.fire("change");
 const wrap=panel.children[6].children[1];
 assert.equal(wrap.children[0].children[2].children.length,12);
 select.value="A";select.fire("change");
 const expected=JSON.parse(scripts[0]).next.groups.filter(g=>g.family==="A").length;
 assert.equal(wrap.children[0].children[2].children.length,expected);
}
assert(html.includes('id="milestone-synthesis"') && html.includes('id="milestone-rankpilot"'));
const synthesis=embedded.latest?.synthesis;
if(synthesis){
 const panel=elems["milestone-synthesis"];
 assert.equal(panel.className,"panel");
 assert.deepEqual(panel.children[1].children.map(n=>n.children[1].textContent),
                  [synthesis.trials,synthesis.oldOracle,synthesis.newOracle,synthesis.better]);
 assert.deepEqual(panel.children[2].children[0].children[2].children.map(r=>r.children.map(c=>c.textContent)),
                  synthesis.families.map(f=>[f.family,f.groups,f.oldOracle,f.newOracle]));
 assert.match(panel.children[3].textContent,/모델 성능이나 새로운 비공개/);
}else{
 assert.equal(elems["milestone-synthesis"].children.length,0,"must not render performance without independent evidence");
}
const pilot=embedded.latest?.rankpilot;
if(pilot){
 const panel=elems["milestone-rankpilot"];
 assert.equal(panel.className,"panel");
 assert.deepEqual(panel.children[1].children[0].children[2].children.map(r=>r.children.map(c=>c.textContent)),pilot.rows);
 assert.match(panel.children[2].textContent,/라이브 AI 모델 호출, 비공개 평가, 채굴 성능을 검증한 결과가 아닙니다/);
}else{
 assert.equal(elems["milestone-rankpilot"].children.length,0,"must not render pilot without independent evidence");
}

const local=embedded.latest?.subprocess_contract;
const localPanel=elems["milestone-local-contract"];
if(local){
 assert.equal(localPanel.hidden,false);
 assert.deepEqual(localPanel.children[1].children[0].children[2].children.map(r=>r.children.map(c=>c.textContent)),local.rows);
 assert.match(localPanel.children[2].textContent,/앱이나 API 제공자의 응답 시간, 비용, 모델 품질은 측정하지 않았습니다/);
}else{
 assert.equal(localPanel.children.length,0,"must not render subprocess card without separate independent evidence");
}

console.log("Dashboard JS: filter combinations, selection, details, counts, baselines and stage state OK; mobile CSS present.");

assert(html.indexOf('id="replay"')<html.indexOf('id="stats-groups"'));
if(!synthesis) assert.match(html,/독립 검토된 절감 기록이 아직 없습니다/);
if(embedded.anytime){
 const choose=elems["replay-case"], repeat=elems["replay-repeat"], list=elems["replay-events"];
 assert.equal(choose.children.length,6);
 assert.equal(elems["replay-ui"].hidden,false);
 assert.match(elems["replay-result"].textContent,/마감 800 ms/);
 choose.value="toy-B1";choose.fire("change");
 repeat.value="2";repeat.fire("change");
 const button=list.children[0].children[0];
 button.fire("click");
 assert.match(elems["replay-detail"].textContent,/toy-B1.*후보 출처.*Z3/);
 assert.equal(button.attributes["aria-pressed"],"true");
 assert.equal(elems["replay-plot"].children[0].attributes.role,"img");
}
