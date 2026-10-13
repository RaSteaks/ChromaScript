import { Button } from "./components";

export const WORKFLOW = [
  { id: "scene", label: "选择场景" },
  { id: "preview", label: "编辑与预览" },
  { id: "delivery", label: "设置交付" },
  { id: "jobs", label: "任务与成果" },
] as const;
export type WorkflowStep = (typeof WORKFLOW)[number]["id"];

/** Named stages communicate order while allowing return visits without losing drafts. */
export function WorkflowNav({
  step,
  onChange,
  descriptions,
}: {
  step: WorkflowStep;
  onChange: (step: WorkflowStep) => void;
  descriptions: string[];
}) {
  return (
    <nav className="workflow-nav" aria-label="创作流程">
      <ol>
        {WORKFLOW.map((item, index) => (
          <li key={item.id}>
            <Button
              className={"workflow-step " + (step === item.id ? "current" : "")}
              aria-current={step === item.id ? "step" : undefined}
              aria-label={`${index + 1}. ${item.label}`}
              onClick={() => onChange(item.id)}
            >
              <span className="step-number" aria-hidden="true">
                {String(index + 1).padStart(2, "0")}
              </span>
              <span className="step-copy">
                <strong>{item.label}</strong>
                <small>{descriptions[index]}</small>
              </span>
            </Button>
          </li>
        ))}
      </ol>
    </nav>
  );
}
