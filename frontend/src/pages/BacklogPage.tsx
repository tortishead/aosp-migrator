import { useParams } from "react-router";

export function BacklogPage() {
  const { workspaceId } = useParams();
  return <h1>Backlog of workspace {workspaceId}</h1>;
}
