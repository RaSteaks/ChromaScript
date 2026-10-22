/** Keep color reference available on demand without crowding the editing surface. */
export function ColorWorkflow() {
  return (
    <details
      className="frame-settings technical-note"
      aria-label="色彩工作流程"
    >
      <summary>色彩工作流程</summary>
      <dl className="specification-summary">
        <div>
          <dt>代码生成与内部工作</dt>
          <dd>ACEScg · AP1 · 线性（固定）</dd>
        </div>
        <div>
          <dt>网页预览</dt>
          <dd>sRGB · ACES 2.0 显示变换</dd>
        </div>
      </dl>
      <p className="technical-note">
        输出色彩空间在交付时选择。统一通过 ACES 2.0
        转换；交付设置不会改变代码生成空间或网页预览。
      </p>
    </details>
  );
}
