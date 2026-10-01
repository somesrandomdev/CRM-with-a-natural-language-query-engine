import { titleCase } from "../format";
import type { Stage } from "../types";

export default function StageChip({ stage }: { stage: Stage }) {
  return <span className={`stage stage-${stage}`}>{titleCase(stage)}</span>;
}
