import "@testing-library/dom";

/* vitest 的 jsdom 环境默认没有 matchMedia —— 而控制台里有两处读它
   （prefers-reduced-motion）。没有这个桩，任何渲染到那两处的测试都会
   报 "window.matchMedia is not a function"，看起来像组件坏了。 */
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}
