import {describe,test,expect} from 'vitest';
import {countCsvRecords} from './useResumableUpload';

describe('countCsvRecords',()=>{
 test('带表头的常规文件扣除表头',()=>{
  const csv='sequence,recorded_at,temperature\n1,t,1.0\n2,t,2.0\n3,t,3.0\n';
  expect(countCsvRecords(csv)).toBe(3);
 });
 test('无表头文件不扣行',()=>{
  const csv='1,t,1.0\n2,t,2.0\n';
  expect(countCsvRecords(csv)).toBe(2);
 });
 test('引号字段内含换行不被误计',()=>{
  const csv='sequence,recorded_at,temperature\n1,t,1.0\n2,t,"a\nb,c"\n3,t,3.0';
  expect(countCsvRecords(csv)).toBe(3);
 });
 test('CRLF 与末尾无换行',()=>{
  expect(countCsvRecords('1,t,1.0\r\n2,t,2.0')).toBe(2);
  expect(countCsvRecords('h1,h2,h3\r\n1,t,1.0\r\n')).toBe(1);
 });
 test('空文件为 0',()=>{
  expect(countCsvRecords('')).toBe(0);
 });
});
