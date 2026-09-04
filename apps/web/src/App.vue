<template>
  <t-config-provider>
    <div id="app"><RouterView /></div>
  </t-config-provider>
</template>

<style>
html, body, #app { width: 100%; height: 100%; margin: 0; padding: 0; font-family: var(--app-font-family, TencentSans, "PingFang SC", "Microsoft YaHei", sans-serif); background: var(--td-bg-color-page); color: var(--td-text-color-primary); }
* { box-sizing: border-box; }
button, input, textarea, select { font: inherit; }
button { cursor: pointer; }
button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible { outline: 2px solid var(--td-brand-color); outline-offset: 2px; }
</style>
