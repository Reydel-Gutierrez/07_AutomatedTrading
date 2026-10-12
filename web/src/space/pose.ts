import { type Pose } from "./Character";
import { type StateKey } from "./state";

/** Body language follows the real state: typing while a run or order is in flight, reading while the CIO answers. */
export function poseFor(key: StateKey): Pose {
  switch (key) {
    case "analyzing":
    case "preparing":
    case "executing":
      return "typing";
    case "reviewing":
      return "reading";
    case "completed":
      return "pleased";
    case "error":
      return "alert";
    default:
      return "idle";
  }
}
