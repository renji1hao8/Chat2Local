function matlab_roundtrip(inPath, outPath)
% Read a PNG produced by Chat2Local, then write a vertically flipped PNG back.
% Writes a result file so the caller never depends on -batch stdout or exit timing.
resultPath = fullfile(fileparts(outPath), 'result.txt');
tmpPath = [resultPath '.tmp'];
fid = fopen(tmpPath, 'w');
try
    img = imread(inPath);
    info = imfinfo(inPath);
    fprintf(fid, 'READ_OK %d %d %d\n', size(img, 1), size(img, 2), size(img, 3));
    fprintf(fid, 'COLORTYPE %s\n', info.ColorType);
    flipped = img(end:-1:1, :, :);
    imwrite(flipped, outPath);
    written = dir(outPath);
    fprintf(fid, 'WROTE_OK %d\n', written.bytes);
catch err
    fprintf(fid, 'ERROR %s\n', err.message);
end
fclose(fid);
movefile(tmpPath, resultPath, 'f');
exit(0);
end
