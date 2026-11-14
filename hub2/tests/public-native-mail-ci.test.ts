import './public-native-mail-ci-guards.test.mjs';
import {test} from 'node:test';
import {fileURLToPath} from 'node:url';
import {executeCi} from './public-native-mail-ci-helper.mjs';

test('public native mail progresses after durable uncertain first effect without healing it',
  {timeout:50000,concurrency:false},async()=>{
    await executeCi(fileURLToPath(new URL('../..',import.meta.url)));
  });
