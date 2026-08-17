/**
 * Monaco 编辑器实例共享引用：EditorPanel 在 onMount 时写入，
 * 大纲/搜索等侧栏面板读取以执行 revealLine + focus。
 */
export const editorRef = { current: null };

/** 定位到指定行（1-based）：居中显示 + 移动光标 + 聚焦。 */
export function revealLine(line) {
  const ed = editorRef.current;
  if (!ed || !line) return false;
  try {
    ed.revealLineInCenterIfOutsideViewport(line);
    ed.setPosition({ lineNumber: line, column: 1 });
    ed.focus();
    return true;
  } catch (e) {
    return false;
  }
}
