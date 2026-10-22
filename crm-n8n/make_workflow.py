"""Сценарий n8n «Чёрный кот: заявка с сайта → amoCRM → Telegram».
python make_workflow.py <telegram chat id> [amo credential id] [telegram credential id]  ->  kurs_lead.json
Импорт: остановить n8n, n8n import:workflow --input=kurs_lead.json, запустить n8n.

Сложная логика — в блоках Code, в остальных блоках только простые ссылки на $json:
выражения вида $('Блок').item ломались после HTTP-запроса (n8n терял связь элементов)."""
import json
import pathlib
import sys

AMO = "https://alexandrikuskos.amocrm.ru"
CHAT_ID = sys.argv[1] if len(sys.argv) > 1 else "YOUR_CHAT_ID"
AMO_CRED = sys.argv[2] if len(sys.argv) > 2 else None
TG_CRED = sys.argv[3] if len(sys.argv) > 3 else None
WORKFLOW_ID = "0bXKDbhFHd5UuZzH"

# id полей и вариантов в amoCRM (настроены через API)
F_TARIFF, F_SOURCE, F_LEVEL, F_GOAL, F_AI_SCORE, F_AI_SUMMARY = 798997, 798999, 799001, 799003, 799007, 799009
F_TG, F_CITY = 799017, 799019
PIPELINE, STATUS_NEW = 11434054, 89554250

NORMALIZE = r"""
// Приводим заявку к одному виду и сразу готовим запрос к ИИ
const b = $input.first().json.body || $input.first().json;
const digits = String(b.phone || '').replace(/\D/g, '');
let phone = digits;
if (digits.length === 11 && (digits[0] === '8' || digits[0] === '7')) phone = '+7' + digits.slice(1);
else if (digits.length === 10) phone = '+7' + digits;
else if (digits) phone = '+' + digits;
if (!b.name || !phone) throw new Error('Заявка без имени или телефона: ' + JSON.stringify(b));
const tariffs = {'сам': 465225, 'с автором': 465227, 'наставник': 465229};
const prices = {'сам': 9900, 'с автором': 19900, 'наставник': 39900};
const levels = {'с нуля': 465241, 'рисую для себя': 465243, 'уже есть заказы': 465245};
const t = String(b.tariff || '').trim().toLowerCase();
const l = String(b.level || '').trim().toLowerCase();
const n = {
  name: String(b.name).trim(), phone, phone10: phone.replace(/\D/g, '').slice(-10),
  telegram: String(b.telegram || '').trim(), city: String(b.city || '').trim(), goal: String(b.goal || '').trim(),
  tariff: t, tariff_enum: tariffs[t] || null, price: prices[t] || 0, level: l, level_enum: levels[l] || null,
};
n.ai_body = {
  model: 'qwen2.5:7b', stream: false, format: 'json', options: {temperature: 0},
  messages: [
    {role: 'system', content: 'Ты помогаешь отделу продаж онлайн-школы иллюстрации. Оцени заявку от 0 до 100: насколько человек готов купить курс (чёткая цель, выбран тариф, есть Telegram, конкретика = выше). Ответь строго JSON: {"score": число, "summary": "1-2 предложения для менеджера: кто это, что хочет, с чего начать разговор"}'},
    {role: 'user', content: `Имя: ${n.name}. Тариф: ${t || 'не выбран'}. Уровень: ${l || 'не указан'}. Цель: ${n.goal || 'не указана'}. Telegram: ${n.telegram ? 'есть' : 'нет'}.`},
  ],
};
return [{json: n}];
"""

MERGE_AI = r"""
// Ответ ИИ + данные заявки. ИИ не ответил — заявка всё равно идёт дальше
const n = $('Нормализация').first().json;
let ai = {score: 0, summary: 'ИИ не ответил'};
try { ai = JSON.parse($input.first().json.message.content); } catch (e) {}
return [{json: {...n, ai_score: Math.max(0, Math.min(100, Number(ai.score) || 0)), ai_summary: String(ai.summary || '')}}];
"""

BUILD_LEAD = r"""
// Сделка: если контакт с таким телефоном уже есть — привязываем его, а не создаём дубль
const n = $('Данные + ИИ').first().json;
// amoCRM отвечает application/hal+json — n8n отдаёт такой ответ строкой в json.data
let found = $input.first().json;
if (typeof found.data === 'string') { try { found = JSON.parse(found.data); } catch (e) { found = {}; } }
const existing = found && found._embedded && found._embedded.contacts && found._embedded.contacts[0];
const cf = [
  {field_id: %(SOURCE)s, values: [{enum_id: 465231}]},
  {field_id: %(AI_SCORE)s, values: [{value: n.ai_score}]},
  {field_id: %(AI_SUMMARY)s, values: [{value: n.ai_summary.slice(0, 1000)}]},
];
if (n.tariff_enum) cf.push({field_id: %(TARIFF)s, values: [{enum_id: n.tariff_enum}]});
if (n.level_enum) cf.push({field_id: %(LEVEL)s, values: [{enum_id: n.level_enum}]});
if (n.goal) cf.push({field_id: %(GOAL)s, values: [{value: n.goal}]});
const contact = existing ? {id: existing.id} : {
  first_name: n.name,
  custom_fields_values: [
    {field_code: 'PHONE', values: [{value: n.phone, enum_code: 'MOB'}]},
    ...(n.telegram ? [{field_id: %(TG)s, values: [{value: n.telegram}]}] : []),
    ...(n.city ? [{field_id: %(CITY)s, values: [{value: n.city}]}] : []),
  ],
};
const lead = {
  name: (n.tariff ? n.tariff[0].toUpperCase() + n.tariff.slice(1) : 'Заявка') + ': ' + n.name,
  price: n.price, pipeline_id: %(PIPELINE)s, status_id: %(STATUS)s, custom_fields_values: cf,
  _embedded: {contacts: [contact], tags: [{name: 'сайт'}, ...(existing ? [{name: 'повторная заявка'}] : [])]},
};
return [{json: {body: [lead], repeat: !!existing}}];
""" % dict(SOURCE=F_SOURCE, AI_SCORE=F_AI_SCORE, AI_SUMMARY=F_AI_SUMMARY, TARIFF=F_TARIFF, LEVEL=F_LEVEL,
           GOAL=F_GOAL, TG=F_TG, CITY=F_CITY, PIPELINE=PIPELINE, STATUS=STATUS_NEW)

MESSAGE = r"""
// Текст для менеджера: горячая (70+) или обычная, ссылка на созданную сделку
const n = $('Данные + ИИ').first().json;
const repeat = $('Сборка сделки').first().json.repeat;
const lead = $input.first().json;
const id = lead.id || (lead._embedded && lead._embedded.leads && lead._embedded.leads[0].id) || '';
const text = [
  (n.ai_score >= 70 ? '🔥 Горячая заявка' : '📝 Новая заявка') + (repeat ? ' (повторная)' : ''),
  '',
  `${n.name}, ${n.phone}` + (n.telegram ? `, ${n.telegram}` : ''),
  `Тариф: ${n.tariff || 'не выбран'}` + (n.city ? ` · ${n.city}` : ''),
  `ИИ-оценка: ${n.ai_score}/100`,
  n.ai_summary,
  '',
  '%(AMO)s/leads/detail/' + id,
].join('\n');
// HTML-режим Telegram: экранируем &, <, > (в Markdown ломались ники с подчёркиванием)
return [{json: {text: text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')}}];
""" % dict(AMO=AMO)

RETRY = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000}


def node(name, typ, ver, pos, params, cred=None, **extra):
    n = {"parameters": params, "name": name, "type": typ, "typeVersion": ver, "position": pos, **extra}
    if cred:
        n["credentials"] = cred
    return n


def amo_http(name, pos, params, **extra):
    cred = {"httpHeaderAuth": {"id": AMO_CRED, "name": "Header Auth account"}} if AMO_CRED else None
    return node(name, "n8n-nodes-base.httpRequest", 4.2, pos,
                {"authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth", **params}, cred, **extra)


nodes = [
    node("Заявка с сайта", "n8n-nodes-base.webhook", 2, [0, 300],
         {"httpMethod": "POST", "path": "kurs-lead", "responseMode": "onReceived", "options": {}}, webhookId="kurs-lead"),
    node("Нормализация", "n8n-nodes-base.code", 2, [200, 300], {"jsCode": NORMALIZE}),
    node("ИИ-оценка", "n8n-nodes-base.httpRequest", 4.2, [400, 300],
         {"method": "POST", "url": "http://127.0.0.1:11434/api/chat", "sendBody": True, "specifyBody": "json",
          "jsonBody": "={{ JSON.stringify($json.ai_body) }}", "options": {"timeout": 180000}},
         continueOnFail=True, alwaysOutputData=True),
    node("Данные + ИИ", "n8n-nodes-base.code", 2, [600, 300], {"jsCode": MERGE_AI}),
    amo_http("Поиск контакта", [800, 300],
             {"url": AMO + "/api/v4/contacts", "sendQuery": True,
              "queryParameters": {"parameters": [{"name": "query", "value": "={{ $json.phone10 }}"}]},
              "options": {"response": {"response": {"neverError": True}}}}, alwaysOutputData=True, **RETRY),
    node("Сборка сделки", "n8n-nodes-base.code", 2, [1000, 300], {"jsCode": BUILD_LEAD}),
    amo_http("Создать сделку в amoCRM", [1200, 300],
             {"method": "POST", "url": AMO + "/api/v4/leads/complex", "sendBody": True, "specifyBody": "json",
              "jsonBody": "={{ JSON.stringify($json.body) }}", "options": {}}, **RETRY),
    node("Текст сообщения", "n8n-nodes-base.code", 2, [1400, 300], {"jsCode": MESSAGE}),
    node("Сообщение менеджеру", "n8n-nodes-base.telegram", 1.2, [1600, 300],
         {"chatId": CHAT_ID, "text": "={{ $json.text }}", "additionalFields": {"appendAttribution": False, "parse_mode": "HTML"}},
         {"telegramApi": {"id": TG_CRED, "name": "Telegram account"}} if TG_CRED else None, **RETRY),
]
conn = {a["name"]: {"main": [[{"node": b["name"], "type": "main", "index": 0}]]} for a, b in zip(nodes, nodes[1:])}

wf = {"id": WORKFLOW_ID, "name": "Чёрный кот: заявка с сайта → amoCRM → Telegram", "nodes": nodes,
      "connections": conn, "settings": {"executionOrder": "v1"}, "active": False}
out = pathlib.Path(__file__).with_name("kurs_lead.json")
out.write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
print("ok", out)
