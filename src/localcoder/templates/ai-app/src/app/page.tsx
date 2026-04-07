import Chat from "@/components/Chat";

export default function Home() {
  return (
    <main>
      <div className="mb-8">
        <h1 className="text-3xl font-bold bg-gradient-to-r from-[var(--accent)] to-[var(--accent2)] bg-clip-text text-transparent">
          {{APP_TITLE}}
        </h1>
        <p className="text-[var(--muted)] mt-2">{{APP_DESCRIPTION}}</p>
      </div>

      <div className="glass rounded-2xl p-6 glow">
        <Chat placeholder="{{PLACEHOLDER}}" />
      </div>

      <p className="text-center text-xs text-[var(--muted)] mt-6">
        Powered by local AI
      </p>
    </main>
  );
}
