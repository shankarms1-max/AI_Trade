import Link from "next/link";

export default function NotFound() {
  return <div className="tm-state" role="alert" style={{ maxWidth: 560 }}>
    <strong>404 · NOT FOUND</strong>
    <span>This page or record does not exist. Records are never invented: an unknown strategy or position id is a 404.</span>
    <span style={{ display: "inline-flex", gap: 10 }}><Link className="tm-btn" href="/">Overview</Link><Link className="tm-btn" href="/strategies">Strategies</Link></span>
  </div>;
}
