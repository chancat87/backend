import os
import tempfile


class CronTab:
    def __init__(self) -> None:
        self.crontab_file = tempfile.NamedTemporaryFile().name
        self.__init__crontab()

    def __init__crontab(self) -> None:
        os.system("crontab -l > {}".format(self.crontab_file))
        with open(self.crontab_file) as f:
            self.crontab_context = f.read()
            self._crontab_context = self.crontab_context

    def list(self):
        crontab_file = tempfile.NamedTemporaryFile().name
        os.system("crontab -l > {}".format(crontab_file))
        with open(crontab_file) as f:
            data = f.read().strip()
            if data:
                data = data.split("\n")
            else:
                data = []
            return data

    def add(
        self,
        schedule_expressions: str,
        cmd: str,
    ) -> None:
        cron = f"{schedule_expressions.strip()}    {cmd.strip()}"
        if cron in self.crontab_context:
            return
        else:
            self.crontab_context += "{}\n".format(cron)

    def remove(
        self,
        schedule_expressions: str,
        cmd: str,
    ) -> None:
        cron = f"{schedule_expressions.strip()}    {cmd.strip()}"
        if cron in self.crontab_context:
            self.crontab_context = self.crontab_context.replace(cron, "")

    def save(self) -> None:
        """
        It will save the crontab to the system.
        """
        self.crontab_context = self.crontab_context.replace("\n\n", "\n")

        with open(self.crontab_file, "w") as f:
            f.write(self.crontab_context)
            self._crontab_context = self.crontab_context
        os.system("crontab {}".format(self.crontab_file))

    def __del__(self):
        if self.crontab_context != self._crontab_context:
            print(
                "Warning: You have modified the crontab, but it is not saved in the system."
            )
        os.remove(self.crontab_file)


if __name__ == "__main__":
    cron = CronTab()
    cron.add("* * * * *", "echo 1")
    cron.save()
    print(cron.list())
    cron.remove("* * * * *", "echo 1")
    cron.add("* * * * *", "echo `date` >> /root/crontab.log")
    cron.remove("*/10 * * * *", "echo `date` >> /root/crontab.log")
    cron.save()
    print(cron.list())