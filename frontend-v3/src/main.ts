import "./styles.css";

import { createApp } from "vue";
import { createPinia } from "pinia";
import {
  ElAlert,
  ElButton,
  ElDialog,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
  ElEmpty,
  ElInput,
  ElOption,
  ElSelect,
  ElTag,
} from "element-plus";

import "element-plus/es/components/alert/style/css";
import "element-plus/es/components/button/style/css";
import "element-plus/es/components/dialog/style/css";
import "element-plus/es/components/dropdown/style/css";
import "element-plus/es/components/empty/style/css";
import "element-plus/es/components/input/style/css";
import "element-plus/es/components/message/style/css";
import "element-plus/es/components/message-box/style/css";
import "element-plus/es/components/option/style/css";
import "element-plus/es/components/select/style/css";
import "element-plus/es/components/tag/style/css";

import App from "./App.vue";
import { router } from "./app/router";

createApp(App)
  .use(createPinia())
  .use(router)
  .use(ElAlert)
  .use(ElButton)
  .use(ElDialog)
  .use(ElDropdown)
  .use(ElDropdownItem)
  .use(ElDropdownMenu)
  .use(ElEmpty)
  .use(ElInput)
  .use(ElOption)
  .use(ElSelect)
  .use(ElTag)
  .mount("#app");
