import js from "@eslint/js";
import vue from "eslint-plugin-vue";
import tseslint from "typescript-eslint";
import vueParser from "vue-eslint-parser";
import globals from "globals";

export default [
  {
    ignores: ["dist/**", "node_modules/**", "e2e/**", "scripts/**"],
  },
  js.configs.recommended,
  ...vue.configs["flat/base"],
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,vue}"],
    languageOptions: {
      globals: globals.browser,
    },
  },
  {
    files: ["src/**/*.vue"],
    languageOptions: {
      parser: vueParser,
      parserOptions: {
        parser: tseslint.parser,
        extraFileExtensions: [".vue"],
      },
    },
  },
  {
    files: ["src/**/*.{ts,vue}"],
    rules: {
      "no-constant-binary-expression": "error",
      "no-debugger": "error",
      "no-duplicate-imports": "error",
      "no-unreachable": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "vue/no-mutating-props": "error",
    },
  },
  {
    // Vue 模板对 script setup 变量的引用由 vue-eslint-parser 解析；
    // 旧组件中仍存在 template ref，当前迁移期不以 TypeScript 的孤立未使用检查误报阻断。
    files: ["src/**/*.vue"],
    rules: {
      "@typescript-eslint/no-unused-vars": "off",
    },
  },
];
