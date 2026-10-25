import { useParams } from "react-router";

export function ItemPage() {
  const { itemKey } = useParams();
  return <h1>{itemKey}</h1>;
}
