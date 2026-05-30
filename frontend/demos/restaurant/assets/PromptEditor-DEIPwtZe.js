import{t as w,G as I,D as L,H as M,q as x,v as z,o as B,h as Y,w as U,y as H,A as T,c as G,e as q,n as V,f as K,p as n,B as N}from"./index-BZfHXsjJ.js";import{T as W}from"./textarea-Cl41phwZ.js";import{R as X}from"./rotate-ccw-9_kQhSju.js";import{S as J}from"./save-B4nM3ZF4.js";import{S as Z}from"./send-BOinENGg.js";import{C as Q}from"./check-pr52Qnl-.js";import{c as ee}from"./createLucideIcon-B583G70S.js";import{C as te}from"./chevron-down-Bzx24684.js";const oe=[["rect",{width:"14",height:"14",x:"8",y:"8",rx:"2",ry:"2",key:"17jyea"}],["path",{d:"M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2",key:"zix9uf"}]],ae=ee("copy",oe),se="pwdemo:clinic:doc",ne=2,A="pwdemo:clinic:doc-change";function D(a){return`${se}:${a}:v${ne}`}function re(a,i){const[u,d]=w.useState(()=>{if(typeof window>"u")return i;try{return localStorage.getItem(D(a))??i}catch{return i}});w.useEffect(()=>{if(typeof window>"u")return;const l=c=>{if(c.detail?.key===a)try{const h=localStorage.getItem(D(a));h!==null&&d(h)}catch{}};return window.addEventListener(A,l),()=>window.removeEventListener(A,l)},[a]);const r=w.useCallback(l=>{d(l);try{localStorage.setItem(D(a),l)}catch{}window.dispatchEvent(new CustomEvent(A,{detail:{key:a}}))},[a]),e=w.useCallback(()=>{d(i);try{localStorage.removeItem(D(a))}catch{}window.dispatchEvent(new CustomEvent(A,{detail:{key:a}}))},[a,i]);return{value:u,set:r,reset:e}}const xe=`# Prime Mate Restaurant — Riyadh (Knowledge Base)

Lebanese restaurant in Al-Olaya, Riyadh. Authentic mezze, grills, and
home-style mains since 2018. 100% halal. Family-friendly. Multilingual
staff (Arabic, English, French, Urdu, Tagalog).

## Identity
- English name: Prime Mate Restaurant
- Arabic name: مطعم برايم ميت
- Cuisine: Lebanese
- Halal certified: yes
- Founded: 2018

## Location & contact
- Address: Al-Olaya district, Riyadh, KSA (العليا، الرياض)
- Phone: +966 11 234 5678
- WhatsApp: +966 50 123 4567
- Email: hello@primematerestaurant.com

## Operating hours
- Saturday – Thursday: 12:00 – 23:30 (last seating 22:30)
- Friday: 13:30 – 23:30 (after Jumu'ah)

## Capacity & seating
- Main dining room: 120 seats indoor
- Outdoor terrace: ~24 seats, Oct–Apr
- Private dining: 2 majlis rooms (small 12 / large 20)

## Delivery
- Zone: within 15 km of Al-Olaya
- Minimum order: SAR 75
- Delivery fee: SAR 20 flat
- ETA: 45–60 min off-peak, 60–90 min during peak (19:30–22:30)

## Payment
- Cash, mada, Visa, Mastercard, Apple Pay in-house.
- Card on file or cash on delivery online.
- VAT 15% included in all menu prices.

## Reservation policy
- Reservations open 30 days in advance.
- Tables held 15 minutes past the booked time.
- Free cancellation 4+ hours before; inside 4 hours may carry a
  SAR 50 per-seat fee at the manager's discretion.

## Menu categories
Cold Mezze, Hot Mezze, Salads, Grills, Main Courses, Desserts,
Beverages. Real items + prices live in the menu module — never
invent dishes.

## Dietary
- 100% halal. No pork, no alcohol, no alcohol-containing ingredients.
- Vegetarian + vegan options across mezze, salads, and mains.
- Common allergens: sesame (tahini), tree nuts, dairy, wheat.
  Severe allergies must be confirmed on arrival.

## Languages spoken
Arabic, English, French, Urdu, Tagalog.

## What we DON'T do
No pork, no alcohol, no shisha. No outside-catered food without
manager approval.
`,we=`# Mate — Restaurant host persona for Prime Mate Restaurant (Riyadh)

You are **Mate** (مايت), the AI host for **Prime Mate Restaurant** /
مطعم برايم ميت — a Lebanese restaurant in Al-Olaya, Riyadh. You
answer phone calls warmly and efficiently. Most callers want to book
a table, place a delivery order, ask about the menu, or ask about
opening hours / location.

## Voice & tone
- Warm, hospitable, concise — like a friendly maître d'.
- Use the caller's first name once they share it.
- Short sentences. One question at a time.
- Smile through your voice — restaurants are about pleasure.

## Arabic gender — default to MASCULINE
- Default to masculine forms ("تفضل", "تقدر"). Switch to feminine
  only after hearing a female voice or a woman's name.

## Language — Arabic by default
- Greet in Arabic (Najdi / Hijazi).
- Detect the caller's language from their first reply and switch
  smoothly (English, French, Urdu, Tagalog, or mixed Arabic-English).

## Greeting (always Arabic)
"السلام عليكم، مطعم برايم ميت، معك مايت. كيف أقدر أخدمك؟"

## You CAN
- Take **reservations** (date, time, party size, seating preference,
  guest name + mobile).
- Take **delivery orders** — quote items + prices ONLY from the
  menu module.
- Quote opening hours, location, delivery zone, minimum order,
  delivery fee, payment methods, halal status — all in the KB.
- Take **catering / event** enquiries; capture name, mobile, date,
  headcount, notes; tell the caller the events team will follow up.

## You MUST NOT
- Never confirm a reservation outside operating hours.
- Never quote a dish, price, or ingredient that isn't in the KB / menu.
- Never promise pork or alcohol — we are 100% halal.
- Never give nutrition or medical advice. For severe allergies, flag
  the kitchen and tell the caller to confirm on arrival with the
  floor manager.

## Tools — use them, don't fake them
- list_menu_categories() — fast nav before listing items.
- list_menu_items(category_id?, search?, max_results?) — BEFORE
  quoting any dish, price, or ingredient.
- list_catering_packages(event_type?, max_guests?) — BEFORE
  quoting any catering option.
- list_available_tables(date, time, party_size?, floor?) — BEFORE
  confirming any reservation slot.
- create_reservation(guest_name, guest_phone, date, time, party_size,
  table_number?, notes?) — ONLY after the caller confirms.
- lookup_catering_order(order_id? | client_phone?) — BEFORE any
  catering follow-up.
- update_catering_order(order_id, event_date?, event_time?, items?,
  guests?, notes?) — \`items\` is a FULL REPLACEMENT (not append).
- flag_for_supervisor(reason, severity?) — silently raise a flag.
- end_call(reason) — see "End of call" below.

## Grounding — sources of truth, priority order
1. Tool results (this call's list_menu_items / list_available_tables /
   lookup_catering_order, etc.). Tool output overrides everything else.
2. The Knowledge Base text above.
If neither covers the question, the truthful answer is
"ما عندي هذه المعلومة، خلني أرجعلك بعد قليل" / "I don't have that
handy — let me check and call you back." NEVER guess.

## End of call — YOU terminate, but ONLY after the caller signals goodbye
Never hang up right after reading back a reservation or order total
— the caller often adds something. Wait for "مع السلامة" / "خلاص
شكراً" / "bye, thanks" or an explicit "no" to "هل تحتاج شي ثاني؟",
then:
1. One-line summary.
2. "شكراً لاتصالك، نتشرف بزيارتك."
3. End the call.
`;function ie({clinics:a,providers:i,appointments:u,overrides:d,lang:r}){const e=[],l=new Date,c=j(l),g=I(l);e.push("# Live clinic state (auto-generated — do NOT edit, refreshes per call)"),e.push(`Generated at: ${l.toISOString()}`),e.push(""),e.push(`## Clinics (${a.length} total)`);for(const t of a){const s=t.head_id?i.find(v=>v.id===t.head_id):null,o=(t.working_hours??L)[M(c)],f=o.open?`${o.open_time}–${o.close_time}${o.break_enabled?` (break ${o.break_start}–${o.break_end})`:""}`:"CLOSED today";e.push(`- ${x(t.name,t.name_ar,r)} · ${x(t.location,t.location_ar,r)} · ${x(t.specialty,t.specialty_ar,r)}`+(s?` · head: ${x(s.name,s.name_ar,r)}`:"")+` · today: ${f}`+(t.active?"":" · INACTIVE"))}e.push("");const h=i.filter(t=>t.active),p={};for(const t of h)p[t.role]=p[t.role]?[...p[t.role],t]:[t];e.push(`## Active staff (${h.length} total)`);for(const t of["doctor","nurse","tech","admin"]){const s=p[t]??[];if(s.length!==0){e.push(`- **${t}** (${s.length}):`);for(const o of s)e.push(`  - ${x(o.name,o.name_ar,r)} · ${x(o.specialty,o.specialty_ar,r)} · ${o.phone}`)}}e.push("");const y=ue(c,a,u,d);e.push(`## Today's totals (${c})`),e.push(`Across all clinics — slots ${y.totalSlots} · booked ${y.booked} · blocked ${y.blocked} · free ${y.free}`),e.push("");const b=7,k=6,S=[];{const t=new Date;t.setHours(0,0,0,0);for(let s=0;s<b;s++){const o=new Date(t);o.setDate(t.getDate()+s),S.push(j(o))}}e.push(`## Free slots — today and next ${b-1} days (per clinic)`);for(const t of a){e.push(`### ${x(t.name,t.name_ar,r)} — ${x(t.specialty,t.specialty_ar,r)}`);for(const s of S){const o=R(t,s,u,d),f=de(s,r);if(o.totalSlots===0){e.push(`- ${f}: closed`);continue}if(o.free===0){e.push(`- ${f}: FULL (${o.booked} booked${o.blocked>0?`, ${o.blocked} blocked`:""})`);continue}const v=o.freeSlots.slice(0,k).join(", "),_=o.freeSlots.length>k?` +${o.freeSlots.length-k} more`:"";e.push(`- ${f}: ${v}${_}  (${o.free} of ${o.totalSlots} free)`)}e.push("")}e.push("## This week totals (per clinic)");for(const t of a){let s=0,o=0,f=0;for(const v of g){const _=R(t,v,u,d);f+=_.totalSlots,s+=_.booked,o+=_.blocked}e.push(`- ${x(t.name,t.name_ar,r)}: slots ${f} · booked ${s} · blocked ${o} · free ${Math.max(0,f-s-o)}`)}e.push("");const m=d.filter(t=>t.date>=c);if(m.length>0){e.push(`## Active slot blocks (${m.length} dates)`);const t=[...m].sort((s,o)=>s.date.localeCompare(o.date));for(const s of t.slice(0,20)){const o=a.find(v=>v.id===s.department_id),f=o?x(o.name,o.name_ar,r):s.department_id;e.push(`- ${s.date} — ${f}: ${s.blocked_slots.length} slots blocked (${s.blocked_slots.slice(0,8).join(", ")}${s.blocked_slots.length>8?"…":""})`)}t.length>20&&e.push(`- …and ${t.length-20} more.`),e.push("")}return e.join(`
`)}function j(a){const i=a.getFullYear(),u=String(a.getMonth()+1).padStart(2,"0"),d=String(a.getDate()).padStart(2,"0");return`${i}-${u}-${d}`}const le=["Sun","Mon","Tue","Wed","Thu","Fri","Sat"],ce=["الأحد","الإثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت"];function de(a,i){const[u,d,r]=a.split("-").map(y=>parseInt(y,10)),e=new Date(u,d-1,r),l=new Date;l.setHours(0,0,0,0);const c=Math.round((e.getTime()-l.getTime())/864e5),g=(i==="ar"?ce:le)[e.getDay()],h=e.toLocaleDateString(i==="ar"?"ar-EG":void 0,{day:"numeric",month:"short"}),p=`${g} ${h}`;return c===0?`Today (${p})`:c===1?`Tomorrow (${p})`:p}function R(a,i,u,d){const r=(a.working_hours??L)[M(i)];if(!r.open)return{totalSlots:0,booked:0,blocked:0,free:0,freeSlots:[]};const e=z(r).filter(m=>!B(m,r)),l=Y(u,i,a.id),c=new Set(d.filter(m=>m.department_id===a.id&&m.date===i).flatMap(m=>m.blocked_slots)),g=new Date,h=j(g),p=g.getHours()*60+g.getMinutes(),y=i===h,b=[];let k=0,S=0;for(const m of e){if(l.has(m)){k++;continue}if(c.has(m)){S++;continue}y&&U(m)<p||b.push(m)}return{totalSlots:e.length,booked:k,blocked:S,free:b.length,freeSlots:b}}function ue(a,i,u,d){let r=0,e=0,l=0,c=0;for(const g of i){const h=R(g,a,u,d);r+=h.totalSlots,e+=h.booked,l+=h.blocked,c+=h.free}return{totalSlots:r,booked:e,blocked:l,free:c}}function ke({heading:a,description:i,storageKey:u,defaultText:d,showLivePreview:r=!0}){const{t:e,lang:l}=H(),{value:c,set:g,reset:h}=re(u,d),[p,y]=w.useState(c);w.useEffect(()=>{y(c)},[c]);const b=p!==c,{items:k}=T("departments",G),{items:S}=T("providers",q),{items:m}=T("appointments",V),{items:t}=T("slot_overrides",K),s=w.useMemo(()=>ie({clinics:k,providers:S,appointments:m,overrides:t,lang:l}),[k,S,m,t,l]),o=`${c.trim()}

${s}`.trim(),[f,v]=w.useState(!1),[_,$]=w.useState("idle"),F=async()=>{try{await navigator.clipboard.writeText(o),v(!0),setTimeout(()=>v(!1),1500)}catch{}},P=async()=>{$("sending");try{const O=await fetch("/api/demo/restaurant/agent/prompt",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(u==="persona"?{persona:c}:u==="kb"?{kb:c}:{})});if(!O.ok)throw new Error(`HTTP ${O.status}`);$("ok"),setTimeout(()=>$("idle"),1500)}catch(E){console.error("Apply to agent failed:",E),$("err"),setTimeout(()=>$("idle"),2500)}};return n.jsxs("div",{className:"space-y-6",children:[n.jsxs("div",{className:"flex flex-wrap items-end justify-between gap-3",children:[n.jsxs("div",{children:[n.jsx("h1",{className:"text-2xl font-semibold tracking-tight text-foreground",children:a}),n.jsx("p",{className:"mt-1 max-w-3xl text-sm text-muted-foreground",children:i})]}),n.jsxs("div",{className:"flex items-center gap-2",children:[b?n.jsxs("span",{className:"text-xs text-amber-600 dark:text-amber-400",children:["● ",e("unsavedChanges")]}):n.jsx("span",{className:"text-xs text-muted-foreground",children:e("saved")}),n.jsxs(N,{variant:"outline",onClick:h,children:[n.jsx(X,{className:"me-2 h-4 w-4"}),e("resetToDefault")]}),n.jsxs(N,{onClick:()=>g(p),disabled:!b,children:[n.jsx(J,{className:"me-2 h-4 w-4"}),e("save")]}),n.jsxs(N,{variant:"outline",onClick:P,disabled:_==="sending"||b,title:e(b?"unsavedChanges":"applyToAgent"),children:[n.jsx(Z,{className:"me-2 h-4 w-4"}),e(_==="ok"?"applied":_==="err"?"applyFailed":"applyToAgent")]})]})]}),n.jsx(C,{title:e("editableSection"),meta:`${p.length.toLocaleString()} chars`,children:n.jsx("div",{className:"p-4",children:n.jsx(W,{value:p,onChange:E=>y(E.target.value),rows:18,dir:"auto",className:"resize-y font-mono text-[12.5px] leading-relaxed"})})}),r&&n.jsx(C,{title:e("liveStatePreview"),meta:`${s.length.toLocaleString()} chars · auto`,children:n.jsx("pre",{className:"max-h-[420px] overflow-auto whitespace-pre-wrap p-4 font-mono text-[12px] leading-relaxed text-muted-foreground",dir:"auto",children:s})}),n.jsx(C,{title:e("compiledPrompt"),meta:`${o.length.toLocaleString()} chars`,headerExtra:n.jsxs(N,{size:"sm",variant:"outline",onClick:E=>{E.stopPropagation(),F()},children:[f?n.jsx(Q,{className:"me-1.5 h-3.5 w-3.5"}):n.jsx(ae,{className:"me-1.5 h-3.5 w-3.5"}),e(f?"copied":"copyPrompt")]}),children:n.jsx("pre",{className:"max-h-[480px] overflow-auto whitespace-pre-wrap p-4 font-mono text-[12px] leading-relaxed text-foreground",dir:"auto",children:o})})]})}function C({title:a,meta:i,headerExtra:u,children:d}){const[r,e]=w.useState(!1);return n.jsxs("div",{className:"overflow-hidden rounded-xl border border-border bg-card",children:[n.jsxs("button",{type:"button",onClick:()=>e(l=>!l),"aria-expanded":r,className:"flex w-full items-center justify-between gap-3 border-b border-border bg-card px-5 py-3 text-start hover:bg-accent/40 transition-colors",children:[n.jsxs("div",{className:"flex items-center gap-2",children:[n.jsx(te,{className:`h-4 w-4 text-muted-foreground transition-transform ${r?"rotate-0":"-rotate-90"}`}),n.jsx("h2",{className:"text-sm font-semibold text-card-foreground",children:a})]}),n.jsxs("div",{className:"flex items-center gap-3",children:[i&&n.jsx("span",{className:"text-xs text-muted-foreground",children:i}),u]})]}),r&&d]})}export{xe as D,ke as P,we as a};
