import { useAuth } from "./AuthContext.jsx";
import LoginPage from "./LoginPage.jsx";

export default function AuthGate({ children }) {
  const { status } = useAuth();

  if (status === "checking") {
    return (
      <div className="auth-screen">
        <div className="auth-checking">
          <strong>REVIVE</strong>
          <span>Checking your secure session...</span>
        </div>
      </div>
    );
  }

  if (status === "signed-out") {
    return <LoginPage />;
  }

  return children;
}
