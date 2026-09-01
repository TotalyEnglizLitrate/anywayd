{config, lib, ...}: let
  cfg = config.programs.anywayd;
in {
  options.programs.anywayd = {
    enable = lib.mkEnableOption "Enable anywayd (installs package and enables systemd service)";
    package = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = "The package to install.";
    };
    systemd.enable = lib.mkOption {
      type = lib.types.bool;
      default = cfg.enable;
      description = "Enable anywayd service, enabled by default based on `programs.anywayd.enable`";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.package != null;
        message = "anywayd.enable is true, but programs.anywayd.package is null. Please specify a package.";
      }
    ];
    environment.systemPackages = [cfg.package];
    systemd.services.anywayd = lib.mkIf cfg.systemd.enable {
      description = "anywayd daemon - background process manager";
      restartTriggers = [cfg.package];
      serviceConfig = {
        ExecStart = lib.getExe' cfg.package "anywayd";
        Restart = "on-failure";
      };
      wantedBy = ["multi-user.target"];
    };
  };
}
