import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './index.css';

// Monaco worker：AMD min 版走 getWorkerUrl 返回绝对路径（workerMain 内部按 label
// 经 require 加载对应 language worker，路径基于 /vendor/monaco/vs）。
// 必须设置成返回字符串而非 new Worker(...)，否则 loader 会对模块 id 做 URL 解析失败
// （Failed to parse URL from /vendor/monaco/vs/language/typescript/tsWorker.js）。
self.MonacoEnvironment = {
  getWorkerUrl: function () {
    return self.location.origin + '/vendor/monaco/vs/base/worker/workerMain.js';
  },
};

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
