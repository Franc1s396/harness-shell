import { Dialog } from "../../components/ui/Dialog";
import { useTranslation } from "react-i18next";

export function AgentImagePreview({src, alt, onClose}: {src: string | null; alt: string; onClose: () => void}) {
  const {t} = useTranslation();
  return <Dialog open={src !== null} title={alt || t("agent.imagePreview")} onClose={onClose}>
    <div className="mt-3 flex justify-end"><button type="button" onClick={onClose}
      className="rounded-md border border-line px-3 py-1 text-ink hover:bg-raised">{t("common.close")}</button></div>
    {src && <img src={src} alt={alt} className="mt-3 max-h-[70vh] w-full object-contain" />}
  </Dialog>;
}
