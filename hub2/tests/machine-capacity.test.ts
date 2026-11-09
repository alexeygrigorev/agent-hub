// Machine session capacity: heartbeat labels are string lists, so the operator
// override travels as a `capacity:8` label; the object form stays accepted for
// direct row writes. Without an override a machine hosts 3 sessions.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { machineCapacity, parseCapacityLabel } from '../src/machine-capacity.ts';

test('machines default to three concurrent sessions', () => {
  assert.equal(machineCapacity('[]'), 3);
  assert.equal(machineCapacity('["os:linux","arch:x86_64"]'), 3);
  assert.equal(machineCapacity('{}'), 3);
  assert.equal(machineCapacity('not json at all'), 3);
});

test('an object capacity row is honoured and clamped to the fleet bound', () => {
  assert.equal(machineCapacity('{"capacity":5}'), 5);
  assert.equal(machineCapacity('{"capacity":99}'), 8);
  assert.equal(machineCapacity('{"capacity":0}'), 3);
  assert.equal(machineCapacity('{"capacity":-2}'), 3);
  assert.equal(machineCapacity('{"capacity":1.5}'), 3);
});

test('a capacity:N heartbeat label raises the limit up to the fleet bound', () => {
  assert.equal(machineCapacity('["os:linux","capacity:8"]'), 8);
  assert.equal(machineCapacity('["capacity=2"]'), 2);
  assert.equal(machineCapacity('["capacity:99"]'), 8, 'the fleet bound clamps operator overrides');
  assert.equal(machineCapacity('["capacity:1"]'), 1);
});

test('malformed capacity labels fall back to the default instead of failing', () => {
  assert.equal(machineCapacity('["capacity:0"]'), 3);
  assert.equal(machineCapacity('["capacity:abc"]'), 3);
  assert.equal(machineCapacity('["capacity:"]'), 3);
  assert.equal(machineCapacity('["capacity"]'), 3);
  assert.equal(machineCapacity('["os:linux",7]'), 3, 'non-string labels are ignored');
});

test('parseCapacityLabel rejects everything but bounded positive integers', () => {
  assert.equal(parseCapacityLabel('capacity:4'), 4);
  assert.equal(parseCapacityLabel('capacity=4'), 4);
  assert.equal(parseCapacityLabel('capacity:999'), 8);
  assert.equal(parseCapacityLabel('capacity:0'), null);
  assert.equal(parseCapacityLabel('capacity:04'), 4, 'digits parse as one integer');
  assert.equal(parseCapacityLabel('Capacity:4'), null);
  assert.equal(parseCapacityLabel('session:4'), null);
  assert.equal(parseCapacityLabel(''), null);
});
