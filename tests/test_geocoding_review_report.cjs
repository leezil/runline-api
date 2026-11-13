const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../toilet_geocoding_review.py'), 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
const input = () => ({ value: '', events: {}, addEventListener(name, fn) { this.events[name] = fn; } });
const query = input(), reason = input(), count = {};
const articles = [
  { textContent: '수원 화장실 주소', dataset: { reason: 'ADDRESS_MISMATCH' } },
  { textContent: '안양 화장실 주소', dataset: { reason: 'ADDRESS_MISMATCH' } },
  { textContent: '수원 역 화장실', dataset: { reason: 'MATCHED_ENTRANCE' } },
];
vm.runInNewContext(script, { document: {
  querySelector: selector => ({ '#query': query, '#reason': reason, '#count': count })[selector],
  querySelectorAll: () => articles,
} });
assert.equal(count.textContent, '3건 표시');
query.value = '수원'; query.events.input();
assert.equal(count.textContent, '2건 표시');
reason.value = 'ADDRESS_MISMATCH'; reason.events.change();
assert.equal(count.textContent, '1건 표시');
assert.equal(articles[0].hidden, false); assert.equal(articles[1].hidden, true);
query.value = '없는시설'; query.events.input();
assert.equal(count.textContent, '0건 표시');
query.value = ''; reason.value = ''; reason.events.change();
assert.equal(count.textContent, '3건 표시');
console.log('PASS: report name/address search, reason filter, combined filters, empty results and reset');
