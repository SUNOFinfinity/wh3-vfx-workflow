param([Parameter(Mandatory)][string]$EditorDir,[Parameter(Mandatory)][string]$Src,[Parameter(Mandatory)][string]$Report,[string]$Request,[string]$Dst)
$ErrorActionPreference='Stop'
[void][Reflection.Assembly]::LoadFrom((Join-Path $EditorDir 'Shared.GameFormats.dll'))
$p=[Shared.GameFormats.AnimationMeta.Parsing.MetaDataFileParser]::new([Shared.GameFormats.AnimationMeta.Parsing.MetaDataDatabase]::new())
$raw=[IO.File]::ReadAllBytes($Src);$m=$p.ParseFile($raw)
function Same([byte[]]$a,[byte[]]$b) {return [Linq.Enumerable]::SequenceEqual($a,$b)}
function Rows($doc) {
 foreach($a in $doc.Attributes){
  $r=[ordered]@{tag=$a.Name;version=$a.Version;decoded=($a.GetType().Name -ne 'ParsedUnknownMetadataAttribute')}
  foreach($f in 'StartTime','EndTime','EffectStartTime','EffectEndTime','NodeIndex','BoneIndex','BoneId','VfxName','Tracking','Scale','Position','Orientation','Id','Filter','EffectType','ScalesWithParent','TerrainMutable','DistanceCulled','SoundEvent','EventName','Value'){
   if($a.PSObject.Properties.Name -contains $f){$v=$a.$f
    if($null -ne $v -and $v.GetType().FullName -match 'Microsoft.Xna.Framework.Vector'){ $v=@(foreach($axis in 'X','Y','Z','W'){if($v.PSObject.Properties.Name -contains $axis){[float]$v.$axis}}) }
    $r[$f]=$v
   }
  }
  foreach($prop in $a.GetType().GetProperties()){
   if(!$r.Contains($prop.Name) -and $prop.Name -notin @('Data','Name','DisplayName','Description','Version') -and ($prop.PropertyType.IsPrimitive -or $prop.PropertyType -eq [string])){$r[$prop.Name]=$prop.GetValue($a)}
  }
  [pscustomobject]$r
 }
}
$round=Same $raw ($p.GenerateBytes($m.Version,$m))
if($Request){
 if(!$round -or $m.Version -ne 2){throw 'No byte-exact v2 metadata round-trip; edits refused'}
 $q=Get-Content -Raw -LiteralPath $Request -Encoding utf8|ConvertFrom-Json
 $match=@($m.Attributes|Where-Object {$_.Name -ceq 'EFFECT' -and $_.VfxName -ceq $q.key})
 if($match.Count -gt 1){throw 'Ambiguous existing key: several event intervals use this effect'}
 if(@($m.Attributes|Where-Object {$_.GetType().Name -eq 'ParsedUnknownMetadataAttribute'}).Count){throw 'Unknown tag/version: cannot exclude a hidden duplicate; no edits'}
 if($match.Count){$a=$match[0];if($a.Version -notin 11,12){throw 'Unsupported existing EFFECT version'}}else{$a=$p.CreateDefault('EFFECT_12');$m.Attributes.Add($a)}
 $a.VfxName=$q.key;$a.StartTime=$q.start;$a.EndTime=$q.end;$a.EffectStartTime=$q.start;$a.EffectEndTime=$q.end;$a.NodeIndex=$q.bone;$a.Tracking=[bool]$q.tracking
 $a.Position=[Microsoft.Xna.Framework.Vector3]::new($q.pos[0],$q.pos[1],$q.pos[2])
 $a.Orientation=[Microsoft.Xna.Framework.Vector4]::new($q.quat[0],$q.quat[1],$q.quat[2],$q.quat[3])
 $out=$p.GenerateBytes($m.Version,$m);$next=$p.ParseFile($out)
 $other=@($next.Attributes|Where-Object {!($_.Name -ceq 'EFFECT' -and $_.VfxName -ceq $q.key)})
 $orig=@(($p.ParseFile($raw)).Attributes|Where-Object {!($_.Name -ceq 'EFFECT' -and $_.VfxName -ceq $q.key)})
 if($other.Count -ne $orig.Count){throw 'Unrelated event count changed'}
 for($i=0;$i -lt $other.Count;$i++){if($other[$i].Name -cne $orig[$i].Name -or !(Same $other[$i].Data $orig[$i].Data)){throw 'Unrelated event bytes changed'}}
 if(!(Same $out ($p.GenerateBytes($next.Version,$next)))){throw 'Edited metadata no longer round-trips'}
 [IO.File]::WriteAllBytes($Dst,$out);$m=$next
}
$json=[ordered]@{meta_version=$m.Version;roundtrip=$round;events=@(Rows $m)}|ConvertTo-Json -Depth 6
[IO.File]::WriteAllText($Report,$json,[Text.UTF8Encoding]::new($false))

