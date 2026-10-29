const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("assets/app.js", "utf8");
const start = source.indexOf("function calculatePublishedContractPricing");
const end = source.indexOf("const syncPublishedContractFees", start);
assert.notEqual(start, -1, "pricing calculator must exist");
assert.notEqual(end, -1, "pricing calculator boundary must exist");
const context = {};
vm.createContext(context);
vm.runInContext(source.slice(start, end) + ";this.calculatePublishedContractPricing = calculatePublishedContractPricing;", context);
const calculate = context.calculatePublishedContractPricing;
const cases = [
  ["", 0, 2400, null, null, "UNSELECTED"],
  ["微信", 0, 2400, 0, 2400, "NOT_APPLICABLE"],
  ["支付宝", 0, 2400, 0, 2400, "NOT_APPLICABLE"],
  ["国内银行", 0, 2400, 0, 2400, "NOT_APPLICABLE"],
  ["澳洲私户", 0, 2400, 0, 2400, "NOT_APPLICABLE"],
  ["澳洲公户", 0, 2400, 240, 2640, "EXCLUSIVE"],
  ["微信", 100, 2300, 0, 2300, "NOT_APPLICABLE"],
  ["澳洲公户", 100, 2300, 230, 2530, "EXCLUSIVE"],
];
for (const [paymentMethod, discountAmount, net, gst, total, treatment] of cases) {
  const actual = calculate({standardFee: 2400, paymentMethod, discountAmount});
  assert.deepEqual(JSON.parse(JSON.stringify(actual)), {net, gst, total, treatment});
}
assert.deepEqual(
  JSON.parse(JSON.stringify(calculate({standardFee: 2400, paymentMethod: "澳洲公户", discountAmount: 0, gstFree: true}))),
  {net: 2400, gst: 0, total: 2400, treatment: "GST_FREE"},
);
console.log("contract published pricing browser matrix: PASS");
