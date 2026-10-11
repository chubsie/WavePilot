import test from 'node:test';
import assert from 'node:assert/strict';
import {
  scaleBox, formatDetectionLabel, formatDetectionLine, formatFrameHeader, detectionSignature,
  summarizeState, formatTrackLine, formatReportHeader, reportSentences, createYoloLog, createThumbnailStore,
} from '../tools/mission_demo/yolo-log.js';

const person = { class_id: 0, class_name: 'person', confidence: .871, xyxy: [64, 36, 128, 108.4], track_id: 3, upper_color: 'red' };
const car = { class_id: 2, class_name: 'car', confidence: .5, xyxy: [0, 0, 10, 10], track_id: null, upper_color: null };
const frameOf = (frameId, detections, extra = {}) => ({ frame_id: frameId, timestamp_sec: 1, image_width: 640, image_height: 360, seq: frameId + 100, detections, ...extra });

test('scaleBox maps source-image pixels onto the view size', () => {
  assert.deepEqual(scaleBox([64, 36, 128, 108], 640, 360, 320, 180), { left: 32, top: 18, width: 32, height: 36 });
  assert.deepEqual(scaleBox(['0', '0', '640', '360'], 640, 360, 160, 90), { left: 0, top: 0, width: 160, height: 90 });
});

test('detection label and log line formats', () => {
  assert.equal(formatDetectionLabel(person), 'person #3 87%');
  assert.equal(formatDetectionLabel(car), 'car 50%');
  assert.equal(formatDetectionLine(person), 'person #3 0.87 [64,36,128,108] red');
  assert.equal(formatDetectionLine(car), 'car 0.50 [0,0,10,10]');
});

test('frame header uses the reported image size, not a hard-coded one', () => {
  assert.equal(formatFrameHeader(frameOf(12, [person, car]), 41.6), 'frame #12 · seq 112 · 640×360 · YOLO 42 ms · 2 det');
  assert.equal(formatFrameHeader(frameOf(1, [], { image_width: 1280, image_height: 720 }), null), 'frame #1 · seq 101 · 1280×720 · YOLO – ms · 0 det');
});

test('detection signature ignores order and box jitter', () => {
  const moved = { ...person, xyxy: [65, 37, 129, 109] };
  assert.equal(detectionSignature(frameOf(1, [person, car])), detectionSignature(frameOf(2, [car, moved])));
  assert.notEqual(detectionSignature(frameOf(1, [person])), detectionSignature(frameOf(2, [car])));
});

const statePayload = {
  schema_version: 2, time_sec: 12.34, drone: {},
  tracks: [
    { track_id: 'T3', class: 'person', position_ned_m: { north: 12.06, east: -3 }, first_seen_sec: 1, last_seen_sec: 12, observation_count: 14, mean_confidence: .862, is_active: true },
    { track_id: 'T4', class: 'person', position_ned_m: { north: 1, east: 2 }, observation_count: 2, mean_confidence: .5, is_active: false },
    { track_id: 'T5', class: 'car', position_ned_m: { north: 0, east: 0 }, observation_count: 1, mean_confidence: .7, is_active: true },
  ],
  counts_by_class: { person: 2, car: 1 }, omitted_track_count: 0,
};

test('state summary and track lines', () => {
  assert.equal(summarizeState(statePayload), 'STATE JSON t=12.3s · tracks 3 (person 2, car 1)');
  assert.equal(summarizeState({ ...statePayload, counts_by_class: undefined }), 'STATE JSON t=12.3s · tracks 3 (person 2, car 1)');
  assert.equal(summarizeState({ time_sec: 0 }), 'STATE JSON t=0.0s · tracks 0');
  assert.equal(formatTrackLine(statePayload.tracks[0]), 'T3 person conf 0.86 obs 14 N 12.1 E -3.0');
  assert.match(formatTrackLine(statePayload.tracks[1]), /\(inactive\)$/);
});

const reportPayload = {
  time_sec: 20, decision: { verdict: 'act', reason: 'red top seen', answers: [] },
  report: { sentences: [{ kind: 'observation', text: 'Person in red near the road.', track_ids: ['T3'] }, { kind: 'suggestion', text: 'Hold position.' }], flags: [], is_grounded: true },
  report_error: null,
};

test('report header and sentences', () => {
  assert.equal(formatReportHeader(reportPayload), 'REPORT t=20.0s · ACT · red top seen');
  assert.deepEqual(reportSentences(reportPayload), [
    { kind: 'observation', text: 'Person in red near the road.', trackIds: ['T3'] },
    { kind: 'suggestion', text: 'Hold position.', trackIds: [] },
  ]);
  assert.deepEqual(reportSentences({ ...reportPayload, report: null }), []);
});

test('log keeps newest first, drops duplicate frame ids and throttles unchanged detections', () => {
  const log = createYoloLog({ maxEntries: 30, frameIntervalMs: 1000 });
  assert.ok(log.addFrame(frameOf(1, [person]), 40, 0));
  assert.equal(log.addFrame(frameOf(1, [person]), 40, 5000), null, 'same frame_id is never added twice');
  assert.equal(log.addFrame(frameOf(2, [person]), 40, 500), null, 'unchanged detections within 1 s are skipped');
  assert.ok(log.addFrame(frameOf(3, [person, car]), 40, 600), 'changed detections are added at once');
  assert.ok(log.addFrame(frameOf(4, [person, car]), 40, 1700), 'unchanged detections after 1 s are added');
  assert.equal(log.addFrame(null, 40, 9000), null);
  assert.deepEqual(log.entries.map((entry) => entry.key), ['frame-4', 'frame-3', 'frame-1']);
  assert.deepEqual(log.entries[1].lines, ['person #3 0.87 [64,36,128,108] red', 'car 0.50 [0,0,10,10]']);
});

test('state and report entries are added once per seq', () => {
  const log = createYoloLog();
  assert.equal(log.addState(null), null);
  assert.equal(log.addState({ seq: 1 }), null, 'missing payload is ignored');
  const state = log.addState({ seq: 1, sent_at: 0, payload: statePayload });
  assert.equal(state.header, 'STATE JSON t=12.3s · tracks 3 (person 2, car 1)');
  assert.equal(JSON.parse(state.json).tracks.length, 3);
  assert.equal(log.addState({ seq: 1, sent_at: 0, payload: statePayload }), null);
  const report = log.addReport({ seq: 7, sent_at: 0, payload: { ...reportPayload, report_error: 'timeout' } });
  assert.equal(report.error, 'timeout');
  assert.equal(log.addReport({ seq: 7, payload: reportPayload }), null);
  assert.ok(log.addReport({ seq: 8, payload: reportPayload }));
  assert.deepEqual(log.entries.map((entry) => entry.key), ['report-8', 'report-7', 'state-1']);
});

test('log is capped and a dropped frame id may be logged again', () => {
  const log = createYoloLog({ maxEntries: 3, frameIntervalMs: 0 });
  for (let id = 1; id <= 5; id++) log.addFrame(frameOf(id, id % 2 ? [person] : [car]), 10, id);
  assert.deepEqual(log.entries.map((entry) => entry.key), ['frame-5', 'frame-4', 'frame-3']);
  assert.ok(log.addFrame(frameOf(1, [car]), 10, 99));
  assert.equal(log.entries.length, 3);
});

test('thumbnail store keeps the newest blobs and revokes evicted urls', () => {
  const revoked = [];
  let next = 0;
  const urls = { createObjectURL: () => `blob:${next++}`, revokeObjectURL: (url) => revoked.push(url) };
  const store = createThumbnailStore(2, urls);
  store.put(10, {}); store.put(11, {}); store.put(12, {});
  assert.equal(store.size, 2);
  assert.equal(store.get(10), null);
  assert.equal(store.get(12), 'blob:2');
  assert.deepEqual(revoked, ['blob:0']);
  store.put(12, {});
  assert.deepEqual(revoked, ['blob:0', 'blob:2']);
  assert.equal(store.get(12), 'blob:3');
});
