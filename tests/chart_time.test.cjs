const test = require("node:test");
const assert = require("node:assert/strict");
const clock = require("../web/chart_time.js");
const stamp = value => Date.parse(value)/1000;
const CN = "Asia/Shanghai", NY = "America/New_York";

test("screenshot 06:25 UTC becomes Beijing 14:25 and New York 02:25", () => {
  const ts = stamp("2026-10-14T06:25:00Z");
  assert.equal(clock.format(ts,CN),"10/14 14:25");
  assert.equal(clock.format(ts,NY),"10/14 02:25");
  assert.match(clock.format(ts,NY,true),/EDT/);
  assert.match(clock.format(ts,NY,true),/^2026-10-14 02:25/);
});
test("winter and summer US open have different Beijing hours", () => {
  const winter = stamp("2026-01-09T14:30:00Z"), summer = stamp("2026-07-08T13:30:00Z");
  assert.equal(clock.format(winter,NY),"01/09 09:30");
  assert.equal(clock.format(summer,NY),"07/08 09:30");
  assert.equal(clock.format(winter,CN),"01/09 22:30");
  assert.equal(clock.format(summer,CN),"07/08 21:30");
  assert.equal(clock.abbreviation(winter),"EST");
  assert.equal(clock.abbreviation(summer),"EDT");
});
test("spring daylight transition skips 02:00", () => {
  assert.equal(clock.format(stamp("2026-03-08T06:59:00Z"),NY),"03/08 01:59");
  assert.equal(clock.format(stamp("2026-03-08T07:00:00Z"),NY),"03/08 03:00");
});
test("fall repeated hour is disambiguated by EDT/EST", () => {
  const first=stamp("2026-11-01T05:30:00Z"), second=stamp("2026-11-01T06:30:00Z");
  assert.equal(clock.format(first,NY),clock.format(second,NY));
  assert.match(clock.format(first,NY,true),/EDT/);
  assert.match(clock.format(second,NY,true),/EST/);
});
test("Beijing date may be one day ahead of New York", () => {
  const ts=stamp("2026-10-14T00:25:00Z");
  assert.equal(clock.format(ts,CN),"10/14 08:25");
  assert.equal(clock.format(ts,NY),"10/13 20:25");
});
test("explicit timezone ignores host timezone and never mutates epochs", () => {
  const ts=stamp("2026-10-14T06:25:00Z"), before=process.env.TZ;
  try {
    process.env.TZ="Pacific/Honolulu";
    assert.equal(clock.format(ts,CN),"10/14 14:25");
    assert.equal(ts,1791959100);
  } finally {
    if (before == null) delete process.env.TZ; else process.env.TZ=before;
  }
});
test("unexpected zones and nonfinite epochs are rejected", () => {
  assert.throws(() => clock.format(0,"local"),RangeError);
  assert.throws(() => clock.format(NaN,CN),RangeError);
});
